"""Saves into structx.sqlite: what the React app sends when a user edits an asset (PUT /assets/{code}) or relabels a
document (PATCH /documents/{id}).

Rules (they follow db_Asset_submission/README.md):
- Only `input_value`, `indication`, `asset` (name, stage), `document_file_type` and `audit_log` are written. The
  cdp / ci / protocol tables are the untouched AI record and are never written here.
- A value the user did not change is left alone (an AI value stays `ai_extracted`). A changed AI value becomes `edited`,
  a value for an empty cell `entered`; both are `source = user` with the confidence columns cleared. A cleared value is deleted.
- Every value is checked against its input's kind and options before anything is written. Bad input -> 422 and nothing is saved.
- One transaction per save (BEGIN IMMEDIATE ... COMMIT); any error rolls everything back.
- The audit log records who changed which input, never the value.
"""
import json
import logging
import math
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException, status

from . import structx_repo as repo
from . import structx_ui as ui
from .structx_map import BY_KEY, from_ui
from .security import CurrentUser

log = logging.getLogger(__name__)

ASSET_CODE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9 ._-]{0,39}$')
IND_ID = re.compile(r'^IND-([1-9]\d{0,8})$')
MAX_INDICATIONS, MAX_COMPETITORS, MAX_EVENTS = 200, 500, 100
CLEAR = object()  # "this cell is empty"


def _bad(message: str) -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, message)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


# ---------------- value checks (by the input's kind)

def _number(v: Any) -> int | float:
    if isinstance(v, bool):
        raise ValueError
    if isinstance(v, str):
        v = float(v.strip())
    if not isinstance(v, (int, float)) or not math.isfinite(v):
        raise ValueError
    return int(v) if float(v).is_integer() else v


def _year(v: Any) -> int:
    n = _number(v)
    if n != int(n) or not 1900 <= n <= 2100:
        raise ValueError
    return int(n)


def _series(v: Any) -> list | object:
    if not isinstance(v, list) or len(v) != 15:
        raise ValueError
    out = [None if x is None or x == '' else _number(x) for x in v]
    if any(x is not None and x < 0 for x in out):
        raise ValueError
    return CLEAR if all(x is None for x in out) else out


def coerce(kind: str, v: Any, options: set[str]) -> Any:
    """The value in the form structx stores it, or CLEAR for an empty cell. Raises ValueError when it is not valid."""
    if v is CLEAR or v is None or v == '' or v == [] or v == {}:
        return CLEAR
    if kind == 'select':
        if not isinstance(v, str):
            raise ValueError
        if v in options:
            return v
        same = [o for o in options if o.lower() == v.lower()]  # e.g. "placebo-controlled" for "Placebo-controlled"
        if len(same) != 1:
            raise ValueError
        return same[0]
    if kind == 'number':
        n = _number(v)
        if n < 0:
            raise ValueError
        return n
    if kind == 'percent':
        n = _number(v)
        if not 0 <= n <= 100:
            raise ValueError
        return n
    if kind == 'year':
        return _year(v)
    if kind == 'year_month':
        if not isinstance(v, dict) or set(v) - {'year', 'month'}:
            raise ValueError
        month = v.get('month')
        if month in (None, ''):
            month = None
        else:
            month = _number(month)
            if month != int(month) or not 1 <= month <= 12:
                raise ValueError
            month = int(month)
        return {'year': _year(v.get('year')), 'month': month}
    if kind == 'series':
        return _series(v)
    if kind == 'series_eu':
        if not isinstance(v, dict) or len(v) > 60:
            raise ValueError
        out = {}
        for country, series in v.items():
            if country not in options:
                raise ValueError
            s = _series(series)
            if s is not CLEAR:
                out[country] = s
        return out or CLEAR
    if kind in ('multi_country', 'multi_select'):
        if not isinstance(v, list) or len(v) > 60 or not all(isinstance(x, str) and x in options for x in v) or len(set(v)) != len(v):
            raise ValueError
        return v
    raise ValueError  # list / calc inputs are never stored


def _same(old: Any, raw: Any) -> bool:
    try:
        return old == raw
    except Exception:  # noqa: BLE001
        return False


# ---------------- transaction helper

def _transaction(conn: sqlite3.Connection, work):
    conn.execute('BEGIN IMMEDIATE')
    try:
        result = work()
        conn.execute('COMMIT')
        return result
    except HTTPException:
        _rollback(conn)
        raise
    except sqlite3.OperationalError as exc:  # most often "database is locked" (another writer)
        _rollback(conn)
        log.exception('structx save failed')
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, 'Your changes could not be saved right now. Please try again.') from exc
    except Exception:
        _rollback(conn)
        raise


def _rollback(conn: sqlite3.Connection) -> None:
    if conn.in_transaction:
        conn.execute('ROLLBACK')


# ---------------- saving an asset

def _text(v: Any, label: str, max_len: int, *, required: bool = False) -> str | None:
    if v is None or v == '':
        if required:
            raise _bad(f'{label} is required.')
        return None
    if not isinstance(v, str) or len(v) > max_len:
        raise _bad(f'{label} is not valid.')
    return v.strip() or None


def save_asset(conn: sqlite3.Connection, user: CurrentUser, asset: sqlite3.Row, body: Any) -> dict:
    """Apply the asset JSON the React app sends and return the asset as the app reads it back."""
    if not isinstance(body, dict) or body.get('id') != asset['name']:
        raise _bad('The asset does not match the address.')
    _transaction(conn, lambda: _apply(conn, user, asset, body))
    return ui.ui_asset(conn, conn.execute('SELECT * FROM asset WHERE asset_id = ?', (asset['asset_id'],)).fetchone())


def _apply(conn: sqlite3.Connection, user: CurrentUser, asset: sqlite3.Row, body: dict) -> None:
    aia = body.get('aia')
    setup = aia.get('setup') if isinstance(aia, dict) else None
    if not isinstance(setup, dict):
        raise _bad('The assessment workspace is missing.')
    inds_in = setup.get('indications') or []
    v6 = aia.get('v6') if isinstance(aia.get('v6'), dict) else {}
    asset_vals, comps = v6.get('asset') or {}, v6.get('comp') or []
    if not isinstance(inds_in, list) or len(inds_in) > MAX_INDICATIONS or not isinstance(asset_vals, dict) \
            or not isinstance(comps, list) or len(comps) > MAX_COMPETITORS:
        raise _bad('The assessment workspace is not valid.')

    now, asset_id = _now(), asset['asset_id']
    su = repo.structx_user(conn, user)
    by = su['user_id'] if su else None
    # The AI team's tables name the editor column `updated_by` (older file) or `user_id` (the extraction package's schema).
    # The name written into the SQL below is one of these two fixed strings, chosen from the table's own columns.
    by_col = 'user_id' if 'updated_by' not in {r['name'] for r in conn.execute('PRAGMA table_info(input_value)')} else 'updated_by'
    events: list[str] = []

    catalogue = {r['id']: r for r in conn.execute('SELECT input_id, id, name, level, kind FROM input')}
    options: dict[int, set[str]] = {}
    for r in conn.execute('SELECT input_id, value FROM input_option'):
        options.setdefault(r['input_id'], set()).add(r['value'])

    # 1. Asset details (the code in `name` never changes)
    changes = {}
    for col, label, value, size, current in (
            ('asset_name', 'Asset name', setup.get('name'), 200, asset['asset_name'] or asset['name']),
            ('stage', 'Stage', setup.get('stage'), 40, asset['stage'] or '')):
        text = _text(value, label, size)
        if text is not None and text != current:
            changes[col] = text
    if changes:
        conn.execute(f'UPDATE asset SET {", ".join(f"{c} = ?" for c in changes)} WHERE asset_id = ?', (*changes.values(), asset_id))
        events.append('updated asset details')

    # 2. Indications (extracted ones are renamed or unticked, never deleted; new ones are added as source = user)
    existing = {r['indication_id']: r for r in conn.execute('SELECT * FROM indication WHERE asset_id = ?', (asset_id,))}
    position = max((r['position'] or 0 for r in existing.values()), default=0)
    known, seen, included = set(existing), set(), 0
    for item in inds_in:
        m = IND_ID.match(str(item.get('id') or '')) if isinstance(item, dict) else None
        if not m or int(m.group(1)) in seen:
            raise _bad('An indication is not valid.')
        n = int(m.group(1))
        seen.add(n)
        name = _text(item.get('name'), 'Indication name', 200, required=True)
        include = item.get('include', True)
        if not isinstance(include, bool):
            raise _bad('An indication is not valid.')
        included += include
        if n in existing:
            row = existing[n]
            if name != row['indication']:
                conn.execute('UPDATE indication SET indication = ? WHERE indication_id = ?', (name, n))
                events.append('renamed indication')
            if int(include) != row['include']:
                conn.execute('UPDATE indication SET "include" = ? WHERE indication_id = ?', (int(include), n))
                events.append('included indication' if include else 'excluded indication')
        else:
            if conn.execute('SELECT 1 FROM indication WHERE indication_id = ?', (n,)).fetchone():
                raise HTTPException(status.HTTP_409_CONFLICT, 'An indication id is already used by another asset.')
            position += 1
            conn.execute('INSERT INTO indication (indication_id, asset_id, indication, "include", source, position) VALUES (?,?,?,?,?,?)',
                         (n, asset_id, name, int(include), 'user', position))
            known.add(n)
            events.append('added indication')
    if inds_in and not included:
        raise _bad('At least one indication must be included.')

    # 3. Values: collect what the app holds, then compare with what structx holds
    desired: list[tuple[str, int | None, str | None, Any]] = []
    for ui_key, raw in asset_vals.items():
        sid = BY_KEY.get(('asset', ui_key))
        if sid:
            desired.append((sid, None, None, raw))
    for item in inds_in:
        n = int(IND_ID.match(item['id']).group(1))
        year = item.get('launchUS')
        desired.append(('us_launch_timing', n, None, CLEAR if year in (None, '') else {'y': year, 'm': item.get('launchUSMonth')}))
        desired.append(('eu_launch_timing', n, None, item.get('launchEU')))
        desired.append(('fda_orphan_drug_designation', n, None, item.get('orphanUS')))
        extra = item.get('v6') if isinstance(item.get('v6'), dict) else {}
        for ui_key, raw in extra.items():
            sid = BY_KEY.get(('ind', ui_key))
            if sid and ui_key not in ('usLaunch', 'euLaunch', 'fdaOdd'):
                desired.append((sid, n, None, raw))
    for comp in comps:
        m = IND_ID.match(str(comp.get('vsId') or '')) if isinstance(comp, dict) else None
        name = _text(comp.get('name'), 'Competitor name', 200, required=True) if m else None
        if not m or int(m.group(1)) not in known:
            raise _bad('A competitor is not valid.')
        for ui_key, raw in comp.items():
            sid = BY_KEY.get(('comp', ui_key))
            if sid:
                desired.append((sid, int(m.group(1)), name, raw))

    for key, ind_id, competitor, raw in desired:
        inp = catalogue.get(key)
        if inp is None or inp['kind'] in ('calc', 'list'):  # not in this catalogue, or never stored
            continue
        row = conn.execute('SELECT * FROM input_value WHERE asset_id = ? AND input_id = ? AND indication_id IS ? AND competitor IS ?',
                           (asset_id, inp['input_id'], ind_id, competitor)).fetchone()
        old = repo._json(row['value']) if row else None
        try:
            raw = from_ui(inp['kind'], raw, CLEAR)
            val = coerce(inp['kind'], raw, options.get(inp['input_id'], set()))
        except ValueError:
            if row and _same(old, raw):  # untouched value the AI team stored in another form: leave it alone
                continue
            raise _bad(f"{inp['name']} is not valid.") from None
        if val is CLEAR:
            if row:
                conn.execute('DELETE FROM input_value WHERE value_id = ?', (row['value_id'],))
                events.append(f"cleared {inp['name']}")
            continue
        text = json.dumps(val, ensure_ascii=False, separators=(',', ':'))
        if row:
            if _same(old, val):
                continue
            conn.execute(
                f'UPDATE input_value SET value = ?, source = ?, status = ?, confidence = NULL, confidence_reason = NULL, citations = NULL, '
                f'document_id = NULL, {by_col} = ?, updated_at = ? WHERE value_id = ?',  # by_col is one of two fixed names, never input
                (text, 'user', 'entered' if row['status'] == 'entered' else 'edited', by, now, row['value_id']))
        else:
            conn.execute(
                f'INSERT INTO input_value (asset_id, input_id, indication_id, competitor, value, source, status, {by_col}, updated_at) '
                'VALUES (?,?,?,?,?,?,?,?,?)', (asset_id, inp['input_id'], ind_id, competitor, text, 'user', 'entered', by, now))
        events.append(f"set {inp['name']}")

    if events:
        conn.execute('UPDATE asset SET updated_at = ? WHERE asset_id = ?', (now, asset_id))
        conn.executemany('INSERT INTO audit_log (user_id, asset_id, event, at) VALUES (?,?,?,?)',
                         [(by, asset_id, e, now) for e in events[:MAX_EVENTS]])


# ---------------- assessment actions

def create_asset(conn: sqlite3.Connection, user: CurrentUser, body: Any) -> dict:
    """POST /assets: add an empty asset (code + optional display name). The user's set-up and inputs are then saved with the
    normal PUT. No file is uploaded and no extraction runs: the AI team's pipeline does that. The creator is granted access."""
    if not isinstance(body, dict):
        raise _bad('The asset is not valid.')
    code = _text(body.get('code'), 'Asset name', 40, required=True)
    if not ASSET_CODE.match(code):
        raise _bad('The asset name may only contain letters, numbers, spaces and . _ -')
    name = _text(body.get('assetName'), 'Asset name', 200) or code
    su = repo.structx_user(conn, user)
    if not user.can_write or (not user.is_admin and not (su and su['can_edit'])):
        raise HTTPException(status.HTTP_403_FORBIDDEN, 'View only for your role.')
    if su is None:  # without a user row the asset would have no owner, no creator and no access grant; refuse instead (fail closed)
        raise HTTPException(status.HTTP_403_FORBIDDEN, 'Your account is not set up in the data source yet. Ask an Admin to add you under User management.')
    by = su['user_id']
    now = _now()

    def work() -> int:
        if conn.execute('SELECT 1 FROM asset WHERE lower(name) = lower(?)', (code,)).fetchone():
            raise HTTPException(status.HTTP_409_CONFLICT, 'An asset with this name already exists.')
        cur = conn.execute('INSERT INTO asset (name, asset_name, owner_user_id, created_by, created_at, updated_at) VALUES (?,?,?,?,?,?)',
                           (code, name, by, by, now, now))
        asset_id = cur.lastrowid
        conn.execute('INSERT INTO user_asset (user_id, asset_id, granted_by, granted_at) VALUES (?,?,?,?)', (by, asset_id, by, now))
        conn.execute('INSERT INTO audit_log (user_id, asset_id, event, at) VALUES (?,?,?,?)', (by, asset_id, 'created asset', now))
        return asset_id

    asset_id = _transaction(conn, work)
    return ui.ui_asset(conn, conn.execute('SELECT * FROM asset WHERE asset_id = ?', (asset_id,)).fetchone())


def record_event(conn: sqlite3.Connection, user: CurrentUser, asset: sqlite3.Row, event: str) -> None:
    """Write one audit row (who did what to which asset; never values). Used for actions that save no data of their own."""
    su = repo.structx_user(conn, user)
    _transaction(conn, lambda: conn.execute('INSERT INTO audit_log (user_id, asset_id, event, at) VALUES (?,?,?,?)',
                                            (su['user_id'] if su else None, asset['asset_id'], event[:200], _now())))


# ---------------- relabelling a document

def save_document_types(conn: sqlite3.Connection, user: CurrentUser, doc_id: int, body: Any) -> dict:
    """PATCH /documents/{id}: structx stores a document's file name, size and labels. Only the labels can change."""
    doc = conn.execute('SELECT d.*, a.name AS asset_code FROM document d JOIN asset a ON a.asset_id = d.asset_id WHERE d.document_id = ?',
                       (doc_id,)).fetchone()
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'Document not found.')
    repo.require_asset_code(conn, user, doc['asset_code'], write=True)
    if not isinstance(body, dict) or body.get('assetId') != doc['asset_code']:
        raise _bad('A document cannot move to another asset.')
    types = body.get('types') or ([body['type']] if isinstance(body.get('type'), str) else [])
    valid = {r['code'] for r in conn.execute('SELECT code FROM file_type')}
    if not isinstance(types, list) or not types or len(types) > 10 or len(set(types)) != len(types) \
            or not all(isinstance(t, str) and t in valid for t in types):
        raise _bad('Choose one or more of the document types.')

    def work() -> None:
        current = [r['type_code'] for r in conn.execute(
            'SELECT type_code FROM document_file_type WHERE document_id = ? ORDER BY position', (doc_id,))]
        if current == types:
            return
        if conn.execute('SELECT 1 FROM document_file_type t JOIN document d ON d.document_id = t.document_id '
                        'WHERE d.asset_id = ? AND d.document_id != ? AND t.position = 1 AND t.type_code = ?',
                        (doc['asset_id'], doc_id, types[0])).fetchone():
            raise HTTPException(status.HTTP_409_CONFLICT, 'Another file of this asset already uses that document type.')
        conn.execute('DELETE FROM document_file_type WHERE document_id = ?', (doc_id,))
        conn.executemany('INSERT INTO document_file_type (document_id, type_code, position) VALUES (?,?,?)',
                         [(doc_id, t, i + 1) for i, t in enumerate(types)])
        su = repo.structx_user(conn, user)
        conn.execute('INSERT INTO audit_log (user_id, asset_id, event, at) VALUES (?,?,?,?)',
                     (su['user_id'] if su else None, doc['asset_id'], f"updated labels for {doc['file_name']}", _now()))

    _transaction(conn, work)
    return ui.ui_document(conn, doc_id)
