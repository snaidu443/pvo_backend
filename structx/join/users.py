"""Roles, users, asset access, sessions and the audit log of the UI. Pure
code. The roles are the mock's three (Admin, Editor, Viewer) with the
permissions its code enforces; a user has one role, a status (active,
pending invite, inactive) and the assets it may open (`user_asset`; an
admin may open every asset). A session is a random token the API hands
out on sign-in, stored as its SHA-256 (never the token), valid 12 hours;
sign-in checks nothing but that the user exists and is active (a demo
stand-in for SSO: anyone holding the app token can sign in as anyone; the
roles are still enforced server-side). The audit log keeps events as text
(who, which asset, what), never values.

    role        code (admin | editor | viewer), name, description, can_edit, can_see_users, can_manage_users, position
    user        user_id, name, email (unique, lowercase), role_code, status, last_active_at, created_at, created_by
    user_asset  user_id, asset_id, granted_by, granted_at
    session     token_hash, user_id, created_at, expires_at
    audit_log   log_id, user_id, asset_id, event, at
"""

from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

ROLE_SEED = (
    ("admin", "Admin", "Manages users and asset access; edits everything.", 1, 1, 1),
    ("editor", "Editor", "Edits inputs and documents of assigned assets; sees users read-only.", 1, 1, 0),
    ("viewer", "Viewer", "Read-only access to assigned assets.", 0, 0, 0),
)
STATUSES = ("active", "pending", "inactive")
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
SESSION_HOURS = 12
# the mock's six synthetic users (initialUsers): names, roles and statuses as there
DEMO_USERS = (
    {"name": "Maya Chen", "email": "maya.chen@takeda.example", "role_code": "admin", "status": "active"},
    {"name": "Thomas Weber", "email": "thomas.weber@takeda.example", "role_code": "editor", "status": "active"},
    {"name": "Aisha Rahman", "email": "aisha.rahman@takeda.example", "role_code": "viewer", "status": "active"},
    {"name": "Daniel Brooks", "email": "daniel.brooks@takeda.example", "role_code": "viewer", "status": "active"},
    {"name": "Elena Rossi", "email": "elena.rossi@takeda.example", "role_code": "editor", "status": "inactive"},
    {"name": "Noah Patel", "email": "noah.patel@takeda.example", "role_code": "viewer", "status": "pending"},
)

USER_TABLES: dict[str, list[tuple[str, str]]] = {
    "role": [("code", "TEXT PRIMARY KEY"), ("name", "TEXT NOT NULL"), ("description", "TEXT NOT NULL"), ("can_edit", "INTEGER NOT NULL"),
             ("can_see_users", "INTEGER NOT NULL"), ("can_manage_users", "INTEGER NOT NULL"), ("position", "INTEGER NOT NULL")],
    "user": [("user_id", "INTEGER PRIMARY KEY"), ("name", "TEXT NOT NULL"), ("email", "TEXT NOT NULL UNIQUE"),
             ("role_code", "TEXT NOT NULL REFERENCES role(code)"), ("status", "TEXT NOT NULL"), ("last_active_at", "TEXT"),
             ("created_at", "TEXT NOT NULL"), ("created_by", "INTEGER REFERENCES user(user_id)")],
    "user_asset": [("user_id", "INTEGER NOT NULL REFERENCES user(user_id)"), ("asset_id", "INTEGER NOT NULL REFERENCES asset(asset_id)"),
                   ("granted_by", "INTEGER REFERENCES user(user_id)"), ("granted_at", "TEXT NOT NULL"), ("PRIMARY KEY (user_id, asset_id)", "")],
    "session": [("token_hash", "TEXT PRIMARY KEY"), ("user_id", "INTEGER NOT NULL REFERENCES user(user_id)"),
                ("created_at", "TEXT NOT NULL"), ("expires_at", "TEXT NOT NULL")],
    "audit_log": [("log_id", "INTEGER PRIMARY KEY"), ("user_id", "INTEGER REFERENCES user(user_id)"),
                  ("asset_id", "INTEGER REFERENCES asset(asset_id)"), ("event", "TEXT NOT NULL"), ("at", "TEXT NOT NULL")],
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(t: datetime) -> str:
    return t.isoformat(timespec="seconds")


def _rows(conn: sqlite3.Connection, sql: str, *args) -> list[dict]:
    cur = conn.execute(sql, args)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def seed_roles(conn: sqlite3.Connection) -> None:
    for position, (code, name, description, edit, see, manage) in enumerate(ROLE_SEED, start=1):
        conn.execute('INSERT INTO "role" (code, name, description, can_edit, can_see_users, can_manage_users, position) VALUES (?, ?, ?, ?, ?, ?, ?) '
                     'ON CONFLICT(code) DO UPDATE SET name = excluded.name, description = excluded.description, can_edit = excluded.can_edit, '
                     'can_see_users = excluded.can_see_users, can_manage_users = excluded.can_manage_users, position = excluded.position',
                     (code, name, description, edit, see, manage, position))


def roles(conn: sqlite3.Connection) -> list[dict]:
    return _rows(conn, 'SELECT * FROM "role" ORDER BY position')


class EmailTaken(ValueError):
    """The email belongs to another user."""


def _check(conn: sqlite3.Connection, *, name: str | None, email: str | None, role_code: str | None, status: str | None,
           asset_ids: list[int] | None, except_id: int | None = None) -> dict:
    out: dict = {}
    if name is not None:
        name = re.sub(r"\s+", " ", name).strip()
        if not name or len(name) > 80:
            raise ValueError("a user name is 1 to 80 characters")
        out["name"] = name
    if email is not None:
        email = email.strip().lower()
        if len(email) > 120 or not EMAIL_RE.match(email):
            raise ValueError("not an email address")
        if conn.execute('SELECT user_id FROM "user" WHERE email = ? AND user_id IS NOT ?', (email, except_id)).fetchone():
            raise EmailTaken(f"a user with the email {email} exists")
        out["email"] = email
    if role_code is not None:
        if role_code not in {r["code"] for r in roles(conn)}:
            raise ValueError(f"unknown role {role_code!r}")
        out["role_code"] = role_code
    if status is not None:
        if status not in STATUSES:
            raise ValueError(f"status must be one of {', '.join(STATUSES)}")
        out["status"] = status
    if asset_ids is not None:
        ids = list(dict.fromkeys(int(a) for a in asset_ids))
        for a in ids:
            if not conn.execute('SELECT 1 FROM "asset" WHERE asset_id = ?', (a,)).fetchone():
                raise ValueError(f"no asset {a}")
        out["asset_ids"] = ids
    return out


def read_user(conn: sqlite3.Connection, user_id: int) -> dict | None:
    """The user with its `role` row, `asset_ids` and `assets` (codes), or None."""
    rows = _rows(conn, 'SELECT * FROM "user" WHERE user_id = ?', user_id)
    if not rows:
        return None
    user = rows[0]
    user["role"] = _rows(conn, 'SELECT * FROM "role" WHERE code = ?', user["role_code"])[0]
    access = _rows(conn, 'SELECT a.asset_id, a.name FROM "user_asset" ua JOIN "asset" a ON a.asset_id = ua.asset_id WHERE ua.user_id = ? ORDER BY a.name', user_id)
    user["asset_ids"] = [a["asset_id"] for a in access]
    user["assets"] = [a["name"] for a in access]
    return user


def _set_assets(conn: sqlite3.Connection, user_id: int, asset_ids: list[int], by: int | None) -> None:
    conn.execute('DELETE FROM "user_asset" WHERE user_id = ?', (user_id,))
    for a in asset_ids:
        conn.execute('INSERT INTO "user_asset" (user_id, asset_id, granted_by, granted_at) VALUES (?, ?, ?, ?)', (user_id, a, by, _iso(_now())))


def create_user(conn: sqlite3.Connection, *, name: str, email: str, role_code: str, status: str = "active",
                asset_ids: list[int] | tuple[int, ...] = (), created_by: int | None = None) -> dict:
    """A new user; ValueError on a bad name, email (unique, lowercase), role, status or asset. Does not commit."""
    f = _check(conn, name=name, email=email, role_code=role_code, status=status, asset_ids=list(asset_ids))
    user_id = conn.execute('INSERT INTO "user" (name, email, role_code, status, created_at, created_by) VALUES (?, ?, ?, ?, ?, ?)',
                           (f["name"], f["email"], f["role_code"], f["status"], _iso(_now()), created_by)).lastrowid
    _set_assets(conn, user_id, f["asset_ids"], created_by)
    return read_user(conn, user_id)


def update_user(conn: sqlite3.Connection, user_id: int, *, name: str | None = None, email: str | None = None, role_code: str | None = None,
                status: str | None = None, asset_ids: list[int] | None = None, by: int | None = None) -> dict:
    if read_user(conn, user_id) is None:
        raise ValueError(f"no user {user_id}")
    f = _check(conn, name=name, email=email, role_code=role_code, status=status, asset_ids=asset_ids, except_id=user_id)
    ids = f.pop("asset_ids", None)
    if f:
        sets = ", ".join(f'"{c}" = ?' for c in f)   # column names are _check's fixed keys
        conn.execute(f'UPDATE "user" SET {sets} WHERE user_id = ?', [*f.values(), user_id])
    if ids is not None:
        _set_assets(conn, user_id, ids, by)
    return read_user(conn, user_id)


def grant_asset(conn: sqlite3.Connection, user_id: int, asset_id: int, by: int | None = None) -> None:
    conn.execute('INSERT OR IGNORE INTO "user_asset" (user_id, asset_id, granted_by, granted_at) VALUES (?, ?, ?, ?)', (user_id, asset_id, by, _iso(_now())))


def list_users(conn: sqlite3.Connection, asset_ids: list[int] | None = None) -> list[dict]:
    """Every user (by user_id), or only those with access to one of `asset_ids`."""
    ids = [r["user_id"] for r in _rows(conn, 'SELECT user_id FROM "user" ORDER BY user_id')]
    if asset_ids is not None:
        if not asset_ids:
            return []
        marks = ", ".join("?" for _ in asset_ids)
        allowed = {r[0] for r in conn.execute(f'SELECT DISTINCT user_id FROM "user_asset" WHERE asset_id IN ({marks})', list(asset_ids))}
        ids = [i for i in ids if i in allowed]
    return [read_user(conn, i) for i in ids]


def sign_in_users(conn: sqlite3.Connection) -> list[dict]:
    """Who can sign in: active and pending users, name and role name only (no emails)."""
    return _rows(conn, 'SELECT u.user_id, u.name, r.name AS role FROM "user" u JOIN "role" r ON r.code = u.role_code '
                       'WHERE u.status IN (\'active\', \'pending\') ORDER BY u.user_id')


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(conn: sqlite3.Connection, user_id: int, hours: int = SESSION_HOURS) -> tuple[str, str]:
    """A new session token for an active or pending user (ValueError otherwise); sweeps expired sessions;
    sets last_active_at. Returns (token, expires_at). Does not commit."""
    user = read_user(conn, user_id)
    if user is None or user["status"] == "inactive":
        raise ValueError("no such active user")
    now = _now()
    conn.execute('DELETE FROM "session" WHERE expires_at < ?', (_iso(now),))
    token = secrets.token_urlsafe(32)
    expires = _iso(now + timedelta(hours=hours))
    conn.execute('INSERT INTO "session" (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)', (_hash(token), user_id, _iso(now), expires))
    conn.execute('UPDATE "user" SET last_active_at = ? WHERE user_id = ?', (_iso(now), user_id))
    return token, expires


def session_user(conn: sqlite3.Connection, token: str) -> dict | None:
    """The user of a live session token (read_user's shape), or None."""
    if not token:
        return None
    row = conn.execute('SELECT user_id FROM "session" WHERE token_hash = ? AND expires_at >= ?', (_hash(token), _iso(_now()))).fetchone()
    if not row:
        return None
    user = read_user(conn, row[0])
    return user if user and user["status"] != "inactive" else None


def delete_session(conn: sqlite3.Connection, token: str) -> None:
    conn.execute('DELETE FROM "session" WHERE token_hash = ?', (_hash(token),))


def audit(conn: sqlite3.Connection, user_id: int | None, asset_id: int | None, event: str) -> None:
    conn.execute('INSERT INTO "audit_log" (user_id, asset_id, event, at) VALUES (?, ?, ?, ?)', (user_id, asset_id, event[:200], _iso(_now())))


def read_audit(conn: sqlite3.Connection, asset_id: int | None = None, limit: int = 20, *, asset_ids: list[int] | None = None) -> list[dict]:
    """The newest events first: event, at, user (name), asset (code). `asset_ids` keeps only events of those assets (an empty list: none)."""
    where, args = "", []
    if asset_id is not None:
        where, args = "WHERE l.asset_id = ?", [asset_id]
    elif asset_ids is not None:
        if not asset_ids:
            return []
        where, args = f"WHERE l.asset_id IN ({','.join('?' * len(asset_ids))})", [int(a) for a in asset_ids]
    return _rows(conn, f'SELECT l.log_id, l.event, l.at, l.user_id, u.name AS user, l.asset_id, a.name AS asset FROM "audit_log" l '
                       f'LEFT JOIN "user" u ON u.user_id = l.user_id LEFT JOIN "asset" a ON a.asset_id = l.asset_id {where} '
                       f'ORDER BY l.log_id DESC LIMIT ?', *args, max(1, min(int(limit), 200)))


def seed_demo_users(conn: sqlite3.Connection) -> int:
    """Inserts the mock's six synthetic users when the user table is empty; the count inserted."""
    if conn.execute('SELECT COUNT(*) FROM "user"').fetchone()[0]:
        return 0
    for u in DEMO_USERS:
        create_user(conn, **u)
    return len(DEMO_USERS)
