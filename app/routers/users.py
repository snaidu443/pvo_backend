"""User management.

Admin: full control. Editor: may invite Editors / Viewers to assets they can access, and add
existing users to those assets (the React "Add user" dialog). Viewer: read-only.
"""
from collections import defaultdict
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import delete, select

from .. import models
from ..permissions import accessible_asset_ids, audit
from ..schemas import UserIn
from ..security import Admin, AnyUser, CurrentUser, DbSession, Writer

router = APIRouter(prefix='/users', tags=['users'])


def _access_map(db: DbSession, user_ids: list[int]) -> dict[int, list[str]]:
    rows = db.execute(select(models.AssetAccess.user_id, models.AssetAccess.asset_id)
                      .where(models.AssetAccess.user_id.in_(user_ids)).order_by(models.AssetAccess.asset_id))
    out: dict[int, list[str]] = defaultdict(list)
    for uid, aid in rows:
        out[uid].append(aid)
    return out


def _out(u: models.User, assets: list[str]) -> dict:
    """The React `User` shape. Columns win over anything in `data`."""
    return {**u.data, 'id': u.id, 'name': u.name, 'email': u.email, 'role': u.role, 'status': u.status,
            'active': u.status == 'active', 'assetIds': assets, 'assignedAssets': assets}


def _one(db: DbSession, u: models.User) -> dict:
    return _out(u, _access_map(db, [u.id]).get(u.id, []))


def _extra(body: UserIn) -> dict:
    return {'last': body.last, 'asset': body.asset, 'roleCode': body.roleCode, 'permissions': body.permissions or []}


def _check_assets_exist(db: DbSession, ids: list[str]) -> None:
    found = set(db.scalars(select(models.Asset.id).where(models.Asset.id.in_(ids)))) if ids else set()
    if found != set(ids):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, 'Unknown asset id.')


def _editor_may_grant(db: DbSession, user: CurrentUser, role: str, asset_ids: set[str]) -> None:
    if role == 'Admin':
        raise HTTPException(status.HTTP_403_FORBIDDEN, 'Only Admin can grant the Admin role.')
    mine = accessible_asset_ids(db, user) or set()
    if not asset_ids <= mine:
        raise HTTPException(status.HTTP_403_FORBIDDEN, 'You can only add users to your own assets.')


def _set_access(db: DbSession, user_id: int, asset_ids: set[str]) -> None:
    db.execute(delete(models.AssetAccess).where(models.AssetAccess.user_id == user_id))
    db.add_all(models.AssetAccess(user_id=user_id, asset_id=a) for a in sorted(asset_ids))


def _email_taken(db: DbSession, email: str, except_id: int | None = None) -> bool:
    stmt = select(models.User.id).where(models.User.email == email)
    if except_id is not None:
        stmt = stmt.where(models.User.id != except_id)
    return db.scalar(stmt) is not None


@router.get('')
def list_users(user: AnyUser, db: DbSession) -> list[dict]:
    """Admin: everyone. Editor: people on the Editor's assets. Viewer: only themselves."""
    stmt = select(models.User).order_by(models.User.id)
    if user.role == 'Editor':
        mine = accessible_asset_ids(db, user) or set()
        shared = select(models.AssetAccess.user_id).where(models.AssetAccess.asset_id.in_(sorted(mine)))
        stmt = stmt.where((models.User.id == user.id) | models.User.id.in_(shared))
    elif user.role == 'Viewer':
        stmt = stmt.where(models.User.id == user.id)
    users = list(db.scalars(stmt))
    access = _access_map(db, [u.id for u in users])
    return [_out(u, access.get(u.id, [])) for u in users]


@router.post('', status_code=201)
def create_user(body: UserIn, user: Writer, db: DbSession) -> dict:
    assets = set(body.assetIds)
    _check_assets_exist(db, sorted(assets))
    status_ = body.status
    if not user.is_admin:
        _editor_may_grant(db, user, body.role, assets)
        status_ = 'pending'  # Editors can only invite
    if db.get(models.User, body.id) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, 'This user id is already used. Refresh and try again.')
    if _email_taken(db, body.email):
        raise HTTPException(status.HTTP_409_CONFLICT, 'A user with this email already exists.')
    row = models.User(id=body.id, name=body.name, email=body.email, role=body.role, status=status_, data=_extra(body),
                      invited_at=datetime.now(timezone.utc) if status_ == 'pending' else None)
    db.add(row)
    db.flush()
    _set_access(db, row.id, assets)
    audit(db, user, 'user.create', f'user:{row.id}')
    db.commit()
    # TODO(okta): assign the user to the PVO Okta app and send the invitation (agree the process with the Okta team).
    return _one(db, row)


@router.put('/{user_id}')
def update_user(user_id: int, body: UserIn, user: Writer, db: DbSession) -> dict:
    if body.id != user_id:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, 'The user id does not match the URL.')
    row = db.get(models.User, user_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'User not found.')
    current = set(_access_map(db, [user_id]).get(user_id, []))
    wanted = set(body.assetIds)
    _check_assets_exist(db, sorted(wanted))

    if not user.is_admin:
        # Editors may only add this user to more of their own assets; nothing else changes.
        if (body.name, body.email, body.role, body.status) != (row.name, row.email, row.role, row.status) or not current <= wanted:
            raise HTTPException(status.HTTP_403_FORBIDDEN, 'Only Admin can change user details.')
        _editor_may_grant(db, user, row.role, wanted - current)
    else:
        if user_id == user.id and (body.role != row.role or body.status != row.status):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, 'You cannot change your own role or status.')
        if _email_taken(db, body.email, except_id=user_id):
            raise HTTPException(status.HTTP_409_CONFLICT, 'A user with this email already exists.')
        row.name, row.email, row.role, row.status = body.name, body.email, body.role, body.status
        row.data = _extra(body)
    _set_access(db, user_id, wanted)
    audit(db, user, 'user.update', f'user:{user_id}')
    db.commit()
    return _one(db, row)


@router.delete('/{user_id}', status_code=204)
def delete_user(user_id: int, user: Admin, db: DbSession) -> None:
    if user_id == user.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, 'You cannot delete your own account.')
    row = db.get(models.User, user_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'User not found.')
    db.execute(delete(models.AssetAccess).where(models.AssetAccess.user_id == user_id))
    db.delete(row)
    audit(db, user, 'user.delete', f'user:{user_id}')
    db.commit()


@router.post('/{user_id}/invite', status_code=204)
def resend_invite(user_id: int, user: Admin, db: DbSession) -> None:
    row = db.get(models.User, user_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'User not found.')
    if row.status != 'pending':
        raise HTTPException(status.HTTP_400_BAD_REQUEST, 'Only pending users can be invited.')
    row.invited_at = datetime.now(timezone.utc)
    audit(db, user, 'user.invite', f'user:{user_id}')
    db.commit()
    # TODO(okta): re-send through the agreed Okta / mail process.
