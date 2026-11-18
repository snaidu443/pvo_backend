"""Read-only access to the AI team's structx.sqlite (the Data Submission tables).

This module is the only place that opens that file and writes SQL. Every query is parameterised.
The file is opened read-only: extraction and user edits are written by the AI team's code, never here.
Tables used: asset, indication, document, document_file_type, file_type, input, input_option, input_value,
user, user_asset (see db_Asset_submission/README.md).
"""
import json
import logging
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from fastapi import HTTPException, status

from .config import get_settings
from .security import CurrentUser

log = logging.getLogger(__name__)

SCREENS = ('submission', 'indications', 'assumptions', 'inputs')
_NOT_STORED_KINDS = ('list', 'calc')  # never stored in input_value, so never "missing"


# ---------------- connection

def _db_path() -> Path:
    raw = get_settings().structx_db_path.strip()
    if not raw:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, 'The data source is not configured.')
    path = Path(raw).resolve()
    if not path.is_file():
        log.error('structx database file not found')
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, 'The data source is not available.')
    return path


def _open(uri: str, **kwargs) -> sqlite3.Connection:
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5, check_same_thread=False, **kwargs)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys = ON')
        return conn
    except sqlite3.Error as exc:
        log.exception('structx database could not be opened')
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, 'The data source is not available.') from exc


def get_structx_conn() -> Iterator[sqlite3.Connection]:
    """FastAPI dependency: one read-only connection per request. Anything unexpected -> 503 (fail closed)."""
    conn = _open(f'{_db_path().as_uri()}?mode=ro')
    try:
        yield conn
    finally:
        conn.close()


def get_structx_rw_conn() -> Iterator[sqlite3.Connection]:
    """FastAPI dependency for the save routes only: a read-write connection. Transactions are started explicitly
    (BEGIN IMMEDIATE in structx_write), so a save either applies completely or not at all."""
    conn = _open(f'{_db_path().as_uri()}?mode=rw', isolation_level=None)
    try:
        yield conn
    finally:
        conn.close()


def open_rw() -> sqlite3.Connection:
    """A read-write connection for work outside a request (the background AI extraction). The caller must close it."""
    return _open(f'{_db_path().as_uri()}?mode=rw', isolation_level=None)


# ---------------- helpers

def _json(text: str | None) -> Any:
    """Parse a JSON text column. Null stays null; text that is not valid JSON is returned as written."""
    if text is None:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return text


def _marks(n: int) -> str:
    return ','.join('?' * n)  # only "?" characters; the values are always bound separately


# ---------------- authorization

def accessible_asset_ids(conn: sqlite3.Connection, user: CurrentUser) -> set[int] | None:
    """Asset ids the user may open, or None for "all" (Admin).

    Others are matched to the structx `user` table by e-mail and must be active there. No match -> no assets.
    """
    if user.is_admin:
        return None
    row = conn.execute('SELECT user_id FROM "user" WHERE lower(email) = ? AND status = ?', (user.email.lower(), 'active')).fetchone()
    if row is None:
        return set()
    return {r['asset_id'] for r in conn.execute('SELECT asset_id FROM user_asset WHERE user_id = ?', (row['user_id'],))}


def require_asset(conn: sqlite3.Connection, user: CurrentUser, asset_id: int) -> sqlite3.Row:
    """The asset row if the user may open it. Unknown and not-permitted both return 404 (no id probing)."""
    ids = accessible_asset_ids(conn, user)
    row = conn.execute('SELECT * FROM asset WHERE asset_id = ?', (asset_id,)).fetchone()
    if row is None or (ids is not None and asset_id not in ids):
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'Asset not found.')
    return row


def structx_user(conn: sqlite3.Connection, user: CurrentUser) -> sqlite3.Row | None:
    """The user's row in structx (matched by e-mail, active only), with the role's edit right."""
    return conn.execute(
        'SELECT u.user_id, r.can_edit FROM "user" u JOIN role r ON r.code = u.role_code WHERE lower(u.email) = ? AND u.status = ?',
        (user.email.lower(), 'active')).fetchone()


def require_asset_code(conn: sqlite3.Connection, user: CurrentUser, code: str, *, write: bool = False) -> sqlite3.Row:
    """The asset with this code if the user may open it (and may edit it when `write`). 404 hides assets the user may not see."""
    row = conn.execute('SELECT * FROM asset WHERE name = ?', (code,)).fetchone()
    ids = accessible_asset_ids(conn, user)
    if row is None or (ids is not None and row['asset_id'] not in ids):
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'Asset not found.')
    if write:
        su = structx_user(conn, user)
        if not user.can_write or (not user.is_admin and not (su and su['can_edit'])):
            raise HTTPException(status.HTTP_403_FORBIDDEN, 'View only for your role.')
    return row


# ---------------- assets and documents

def asset_out(row: sqlite3.Row) -> dict:
    return {
        'assetId': row['asset_id'], 'name': row['name'], 'assetName': row['asset_name'] or row['name'],
        'stage': row['stage'],
        'createdAt': row['created_at'], 'updatedAt': row['updated_at'],
        'extractedAt': row['extracted_at'], 'model': row['model'], 'seconds': row['seconds'],
        'files': {'cdp': row['cdp_file'], 'ci': row['ci_file'], 'protocol': row['protocol_file']},
    }


def list_assets(conn: sqlite3.Connection, user: CurrentUser) -> list[dict]:
    ids = accessible_asset_ids(conn, user)
    if ids is None:
        rows = conn.execute('SELECT * FROM asset ORDER BY name').fetchall()
    elif not ids:
        return []
    else:
        rows = conn.execute(f'SELECT * FROM asset WHERE asset_id IN ({_marks(len(ids))}) ORDER BY name', sorted(ids)).fetchall()
    return [asset_out(r) for r in rows]


def list_documents(conn: sqlite3.Connection, asset_id: int) -> list[dict]:
    rows = conn.execute(
        'SELECT d.document_id, d.file_name, d.size_bytes, d.suffix, d.macro_enabled, d.uploaded_at, d.extracted_at, '
        't.type_code, t.position, f.label, f.spec '
        'FROM document d JOIN document_file_type t ON t.document_id = d.document_id '
        'JOIN file_type f ON f.code = t.type_code WHERE d.asset_id = ? ORDER BY d.document_id, t.position',
        (asset_id,)).fetchall()
    docs: dict[int, dict] = {}
    for r in rows:
        doc = docs.setdefault(r['document_id'], {
            'documentId': r['document_id'], 'fileName': r['file_name'], 'sizeBytes': r['size_bytes'], 'suffix': r['suffix'],
            'macroEnabled': bool(r['macro_enabled']), 'uploadedAt': r['uploaded_at'], 'extractedAt': r['extracted_at'],
            'primaryType': None, 'types': [], 'extractionSpec': None})
        doc['types'].append(r['type_code'])
        if r['position'] == 1:
            doc['primaryType'], doc['extractionSpec'] = r['type_code'], r['spec']
    return list(docs.values())


def extraction_status(conn: sqlite3.Connection, asset: sqlite3.Row) -> dict:
    """Best available progress today: there is no extraction_run table yet, so only "extracted" or "not yet" is knowable.

    A running or a failed extraction cannot be told apart from a not-yet-started one until the AI team adds that table.
    """
    docs = list_documents(conn, asset['asset_id'])
    per_type = [{'type': d['primaryType'], 'fileName': d['fileName'], 'spec': d['extractionSpec'], 'extractedAt': d['extractedAt'],
                 'state': 'extracted' if d['extractedAt'] else 'not_extracted'} for d in docs if d['extractionSpec']]
    return {'assetExtractedAt': asset['extracted_at'], 'documents': per_type,
            'allExtracted': bool(per_type) and all(p['state'] == 'extracted' for p in per_type),
            'precise': False}  # becomes True once extraction_run exists


# ---------------- lookups (options for the set-up dropdowns)

def option_lists(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """Every input's option list from the catalogue (`input_option`), keyed by the structx input id, in the catalogue's order."""
    out: dict[str, list[str]] = {}
    for r in conn.execute('SELECT i.id, o.value FROM input_option o JOIN input i ON i.input_id = o.input_id ORDER BY i.input_id, o.position'):
        out.setdefault(r['id'], []).append(r['value'])
    return out


# ---------------- indications

def list_indications(conn: sqlite3.Connection, asset_id: int) -> list[dict]:
    rows = conn.execute('SELECT * FROM indication WHERE asset_id = ? ORDER BY position, indication_id', (asset_id,)).fetchall()
    return [{'indicationId': r['indication_id'], 'indication': r['indication'], 'include': bool(r['include']), 'source': r['source'],
             'position': r['position'], 'confidence': r['confidence'], 'confidenceReason': r['confidence_reason'],
             'citations': _json(r['citations'])} for r in rows]


# ---------------- inputs / assumptions / submission

def _value_out(r: sqlite3.Row) -> dict:
    return {'valueId': r['value_id'], 'indicationId': r['indication_id'], 'competitor': r['competitor'], 'value': _json(r['value']),
            'source': r['source'], 'status': r['status'], 'confidence': r['confidence'], 'confidenceReason': r['confidence_reason'],
            'citations': _json(r['citations']), 'documentId': r['document_id'], 'updatedAt': r['updated_at']}


def _missing_indications(inp: sqlite3.Row, values: list[dict], included: list[int]) -> list[int]:
    """Included indications that have no value for an indication-level asset input (empty for other kinds of input)."""
    if inp['section'] != 'asset' or inp['level'] != 'indication':
        return []
    have = {v['indicationId'] for v in values}
    return [i for i in included if i not in have]


def _is_missing(inp: sqlite3.Row, values: list[dict], included: list[int]) -> bool:
    if inp['kind'] in _NOT_STORED_KINDS:
        return False
    if inp['section'] == 'asset' and inp['level'] == 'indication':
        # No included indication means nothing has been extracted or confirmed yet: that is missing, not complete.
        return not included or bool(_missing_indications(inp, values, included))
    return not values


def launch_geographies(conn: sqlite3.Connection, asset_id: int) -> set[str] | None:
    """The geographies in the asset's launch scope (Launch Markets on Data Submission: US -> Global + US, EU -> Global + EU,
    otherwise all), decided by the AI package. None when the database has no `geography` column (an older file): no filtering."""
    try:
        if 'geography' not in {r[1] for r in conn.execute('PRAGMA table_info(input)')}:
            return None
        from structx.join.inputs import launch_scope
        return set(launch_scope(conn, asset_id)['geographies'])
    except (ImportError, sqlite3.Error, KeyError, ValueError):
        log.error('the launch scope of the asset could not be read; its inputs are not filtered by market')
        return None


def screen_items(conn: sqlite3.Connection, asset_id: int, screen: str) -> list[dict]:
    """The input catalogue of one screen joined with the asset's values. Empty inputs are kept (the UI shows them as missing).
    Inputs outside the asset's launch scope (for example EU inputs of a US-only launch) are left out."""
    scope = launch_geographies(conn, asset_id)
    inputs = [r for r in conn.execute('SELECT * FROM input WHERE screen = ? ORDER BY workbook_row, input_id', (screen,))
              if scope is None or 'geography' not in r.keys() or r['geography'] in scope]
    options: dict[int, list[str]] = {}
    for r in conn.execute(
            'SELECT o.input_id, o.value FROM input_option o JOIN input i ON i.input_id = o.input_id '
            'WHERE i.screen = ? ORDER BY o.input_id, o.position', (screen,)):
        options.setdefault(r['input_id'], []).append(r['value'])
    values: dict[int, list[dict]] = {}
    for r in conn.execute(
            'SELECT v.* FROM input_value v JOIN input i ON i.input_id = v.input_id '
            'WHERE v.asset_id = ? AND i.screen = ? ORDER BY v.indication_id, v.competitor, v.value_id', (asset_id, screen)):
        values.setdefault(r['input_id'], []).append(_value_out(r))
    included = [r['indication_id'] for r in conn.execute(
        'SELECT indication_id FROM indication WHERE asset_id = ? AND "include" = 1 ORDER BY position, indication_id', (asset_id,))]

    items = []
    for inp in inputs:
        vals = values.get(inp['input_id'], [])
        items.append({
            'input': {'inputId': inp['input_id'], 'id': inp['id'], 'name': inp['name'], 'section': inp['section'], 'level': inp['level'],
                      'geography': inp['geography'] if 'geography' in inp.keys() else None,
                      'kind': inp['kind'], 'source': inp['source'], 'required': bool(inp['required']), 'unit': inp['unit'],
                      'expected': inp['expected'], 'explanation': inp['explanation'], 'policies': _json(inp['policies']) or [],
                      'options': options.get(inp['input_id'], [])},
            'values': vals,
            'missing': _is_missing(inp, vals, included),
            'missingIndicationIds': _missing_indications(inp, vals, included),
        })
    return items


def coverage(conn: sqlite3.Connection, asset_id: int) -> dict:
    """Required inputs filled / total, per screen. "Filled" follows _is_missing (see its rules)."""
    out: dict[str, dict] = {}
    for screen in SCREENS:
        required = [i for i in screen_items(conn, asset_id, screen) if i['input']['required'] and i['input']['kind'] not in _NOT_STORED_KINDS]
        missing = [i for i in required if i['missing']]
        out[screen] = {'required': len(required), 'filled': len(required) - len(missing),
                       'missing': [i['input']['id'] for i in missing]}
    return out
