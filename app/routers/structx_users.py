"""User management on the AI team's structx.sqlite (tables: user, role, user_asset, audit_log).

    GET    /structx/users                   the users the caller may see
    GET    /structx/users/roles             the roles and what each may do
    GET    /structx/users/{id}              one user
    POST   /structx/users                   add a user                       (manage-users right)
    PUT    /structx/users/{id}              change name, email, role, status and assets   (manage-users right)
    PUT    /structx/users/{id}/assets       replace the user's assets        (manage-users right)
    PATCH  /structx/users/{id}/status       activate / deactivate            (manage-users right)
    POST   /structx/users/{id}/invite       resend the invitation of a pending user   (manage-users right)

The rows are read and written with the AI package's own functions (structx.join.users), so the tables are filled exactly
as the package defines them. The output has the shape of the React `User` (assetIds are asset codes, structx `asset.name`).

Who may do what comes from the caller's row in structx (matched by e-mail, active only) and its role's flags:
  can_manage_users (Admin)       every user; add, change, activate / deactivate
  can_see_users (Editor)         themselves and the users who share one of their assets; read only
  neither (Viewer)               only themselves
No match or an inactive caller is refused (fail closed). Users are never deleted: deactivate instead, because the audit
log, the uploaded documents and the saved values point to them.

Security notes: every query is parameterised; the audit log gets event text with ids only (no names or e-mails);
a user's own role and status cannot be changed here.
"""
import sqlite3
from typing import Annotated, Literal, NamedTuple

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from structx.join import users as U

from .. import structx_repo as repo
from ..security import AnyUser, CurrentUser
from ..structx_write import _transaction

router = APIRouter(prefix='/structx/users', tags=['structx users'])

Conn = Annotated[sqlite3.Connection, Depends(repo.get_structx_conn)]
RwConn = Annotated[sqlite3.Connection, Depends(repo.get_structx_rw_conn)]

RoleName = Literal['Admin', 'Editor', 'Viewer']
Status = Literal['active', 'inactive', 'pending']


class UserBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
    email: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=120)]
    role: RoleName
    status: Status = 'pending'
    assetIds: Annotated[list[Annotated[str, StringConstraints(min_length=1, max_length=80)]], Field(max_length=500)] = []


class AssetsBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    assetIds: Annotated[list[Annotated[str, StringConstraints(min_length=1, max_length=80)]], Field(max_length=500)]


class StatusBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    status: Literal['active', 'inactive']


# ---------------- who is asking

class Caller(NamedTuple):
    user_id: int
    manage: bool
    see: bool


def _caller(conn: sqlite3.Connection, user: CurrentUser) -> Caller:
    row = conn.execute('SELECT u.user_id, r.can_manage_users, r.can_see_users FROM "user" u JOIN role r ON r.code = u.role_code '
                       'WHERE lower(u.email) = ? AND u.status = ?', (user.email.lower(), 'active')).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, 'Your account does not have access to user management.')
    return Caller(row['user_id'], bool(row['can_manage_users']), bool(row['can_see_users']))


def _manager(conn: sqlite3.Connection, user: CurrentUser) -> Caller:
    c = _caller(conn, user)
    if not c.manage:
        raise HTTPException(status.HTTP_403_FORBIDDEN, 'Only Admin can manage users.')
    return c


def _shares_asset(conn: sqlite3.Connection, a: int, b: int) -> bool:
    return conn.execute('SELECT 1 FROM user_asset x JOIN user_asset y ON y.asset_id = x.asset_id WHERE x.user_id = ? AND y.user_id = ? LIMIT 1',
                        (a, b)).fetchone() is not None


def _may_see(conn: sqlite3.Connection, c: Caller, target_id: int) -> bool:
    return c.manage or target_id == c.user_id or (c.see and _shares_asset(conn, c.user_id, target_id))


def _visible(conn: sqlite3.Connection, c: Caller) -> list[dict]:
    if c.manage:
        return U.list_users(conn)
    if c.see:
        mine = [r['asset_id'] for r in conn.execute('SELECT asset_id FROM user_asset WHERE user_id = ?', (c.user_id,))]
        users = {u['user_id']: u for u in U.list_users(conn, mine)}
        me = U.read_user(conn, c.user_id)
        if me:
            users[me['user_id']] = me
        return [users[i] for i in sorted(users)]
    me = U.read_user(conn, c.user_id)
    return [me] if me else []


# ---------------- shapes and checks

def _out(u: dict) -> dict:
    """The React `User` shape."""
    last = u['last_active_at']
    return {'id': u['user_id'], 'name': u['name'], 'email': u['email'], 'role': u['role']['name'], 'roleCode': u['role_code'],
            'status': u['status'], 'active': u['status'] == 'active', 'assetIds': u['assets'], 'assignedAssets': u['assets'],
            'last': last[:10] if last else 'Never', 'lastActiveAt': last, 'createdAt': u['created_at'],
            'asset': ', '.join(u['assets']) or 'No assets assigned',
            'permissions': [name for flag, name in (('can_edit', 'edit'), ('can_see_users', 'see_users'), ('can_manage_users', 'manage_users')) if u['role'][flag]]}


def _role_code(conn: sqlite3.Connection, name: str) -> str:
    codes = {r['name']: r['code'] for r in U.roles(conn)}
    if name not in codes:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, 'Unknown role.')
    return codes[name]


def _asset_ids(conn: sqlite3.Connection, codes: list[str]) -> list[int]:
    wanted = list(dict.fromkeys(codes))
    if not wanted:
        return []
    marks = ','.join('?' * len(wanted))  # only "?" characters; the codes are bound separately
    found = {r['name']: r['asset_id'] for r in conn.execute(f'SELECT asset_id, name FROM asset WHERE name IN ({marks})', wanted)}
    if len(found) != len(wanted):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, 'Unknown asset.')
    return [found[c] for c in wanted]


def _guard_change(c: Caller, target: dict, *, role_code: str, new_status: str) -> None:
    """Refuse changing your own role or status. (The caller is an active Admin who is not the target, so at least one active
    Admin always remains.)"""
    if (role_code != target['role_code'] or new_status != target['status']) and target['user_id'] == c.user_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, 'You cannot change your own role or status.')


def _target(conn: sqlite3.Connection, user_id: int) -> dict:
    u = U.read_user(conn, user_id)
    if u is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'User not found.')
    return u


def _checked(fn, *args, **kwargs):
    """Call a package function; its validation errors become fixed 4xx messages (the submitted values are not echoed)."""
    try:
        return fn(*args, **kwargs)
    except U.EmailTaken:
        raise HTTPException(status.HTTP_409_CONFLICT, 'A user with this email already exists.') from None
    except ValueError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, 'Some user details are not valid.') from None


# ---------------- routes

@router.get('/roles')
def list_roles(user: AnyUser, conn: Conn) -> list[dict]:
    _caller(conn, user)
    return [{'code': r['code'], 'name': r['name'], 'description': r['description'], 'canEdit': bool(r['can_edit']),
             'canSeeUsers': bool(r['can_see_users']), 'canManageUsers': bool(r['can_manage_users'])} for r in U.roles(conn)]


@router.get('')
def list_users(user: AnyUser, conn: Conn) -> list[dict]:
    return [_out(u) for u in _visible(conn, _caller(conn, user))]


@router.get('/{user_id}')
def get_user(user_id: int, user: AnyUser, conn: Conn) -> dict:
    c = _caller(conn, user)
    if not _may_see(conn, c, user_id):  # unknown and not-permitted look the same
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'User not found.')
    return _out(_target(conn, user_id))


@router.post('', status_code=status.HTTP_201_CREATED)
def create_user(body: UserBody, user: AnyUser, conn: RwConn) -> dict:
    def work() -> dict:
        c = _manager(conn, user)
        created = _checked(U.create_user, conn, name=body.name, email=body.email, role_code=_role_code(conn, body.role),
                           status=body.status, asset_ids=_asset_ids(conn, body.assetIds), created_by=c.user_id)
        U.audit(conn, c.user_id, None, f'user {created["user_id"]} created')
        # TODO(okta): assign the user to the PVO Okta app and send the invitation (agree the process with the Okta team).
        return created

    return _out(_transaction(conn, work))


@router.put('/{user_id}')
def update_user(user_id: int, body: UserBody, user: AnyUser, conn: RwConn) -> dict:
    def work() -> dict:
        c = _manager(conn, user)
        target = _target(conn, user_id)
        role_code = _role_code(conn, body.role)
        _guard_change(c, target, role_code=role_code, new_status=body.status)
        updated = _checked(U.update_user, conn, user_id, name=body.name, email=body.email, role_code=role_code, status=body.status,
                           asset_ids=_asset_ids(conn, body.assetIds), by=c.user_id)
        U.audit(conn, c.user_id, None, f'user {user_id} updated')
        return updated

    return _out(_transaction(conn, work))


@router.put('/{user_id}/assets')
def set_assets(user_id: int, body: AssetsBody, user: AnyUser, conn: RwConn) -> dict:
    def work() -> dict:
        c = _manager(conn, user)
        _target(conn, user_id)
        updated = _checked(U.update_user, conn, user_id, asset_ids=_asset_ids(conn, body.assetIds), by=c.user_id)
        U.audit(conn, c.user_id, None, f'assets of user {user_id} changed')
        return updated

    return _out(_transaction(conn, work))


@router.patch('/{user_id}/status')
def set_status(user_id: int, body: StatusBody, user: AnyUser, conn: RwConn) -> dict:
    def work() -> dict:
        c = _manager(conn, user)
        target = _target(conn, user_id)
        _guard_change(c, target, role_code=target['role_code'], new_status=body.status)
        updated = _checked(U.update_user, conn, user_id, status=body.status, by=c.user_id)
        U.audit(conn, c.user_id, None, f'user {user_id} {"activated" if body.status == "active" else "deactivated"}')
        return updated

    return _out(_transaction(conn, work))


@router.post('/{user_id}/invite', status_code=status.HTTP_204_NO_CONTENT)
def resend_invite(user_id: int, user: AnyUser, conn: RwConn) -> None:
    def work() -> None:
        c = _manager(conn, user)
        if _target(conn, user_id)['status'] != 'pending':
            raise HTTPException(status.HTTP_409_CONFLICT, 'Only a pending user can be invited again.')
        U.audit(conn, c.user_id, None, f'invitation of user {user_id} resent')
        # TODO(okta): send the invitation again once the Okta process is agreed.

    _transaction(conn, work)
