"""Adding a source document to an asset: POST /assets/{code}/documents.

The file is checked, saved in the upload folder (PVO_UPLOAD_DIR) and registered in structx.sqlite (`document` +
`document_file_type`) in one transaction, so the AI team's pipeline finds both. If anything fails nothing is left behind.

Where the file lands:   <PVO_UPLOAD_DIR>/<asset code>/<file name>
structx's `document` table has no path column, so this folder convention is what the AI team's pipeline must read from.

Checks (server side; the browser checks the same things only to give faster feedback):
- the document type must exist in `file_type`, and the file extension must be one of that type's accepted extensions;
- size 1 byte .. PVO_MAX_UPLOAD_BYTES; the first bytes must match the extension (PDF / Office zip / old Office);
- the file name is reduced to a safe name, never used as a path; macro-enabled files (.xlsm) are stored but flagged and never opened;
- one file per document type per asset (as in the extraction tables); the same file name twice is refused;
- the folder is fixed by configuration; nothing the client sends decides where the file is written.
There is no malware scan here. The folder must be scanned by the platform's scanner before the pipeline opens anything in it.
"""
import json
import logging
import re
import sqlite3
from pathlib import Path
from typing import BinaryIO

from fastapi import HTTPException, status

from . import structx_repo as repo
from . import structx_ui as ui
from .config import get_settings
from .security import CurrentUser
from .structx_write import _bad, _now, _transaction

log = logging.getLogger(__name__)

_CHUNK = 1024 * 1024
_SAFE_NAME = re.compile(r'[^A-Za-z0-9 ._()\-]')
_ZIP, _OLE, _PDF = b'PK\x03\x04', b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1', b'%PDF'
_MAGIC = {'.pdf': (_PDF,), '.docx': (_ZIP,), '.pptx': (_ZIP,), '.xlsx': (_ZIP,), '.xlsm': (_ZIP,),
          '.doc': (_OLE,), '.ppt': (_OLE,), '.xls': (_OLE,)}


def _root() -> Path:
    raw = get_settings().upload_dir.strip()
    if not raw:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, 'Document upload is not configured.')
    return Path(raw).resolve()


def asset_folder(root: Path, asset_name: str) -> Path:
    """The folder of one asset inside the upload root. The name is reduced to safe characters, so it is never a path."""
    return root / re.sub(r'[^A-Za-z0-9._-]', '_', asset_name)


def ensure_path_column(conn: sqlite3.Connection, root: Path) -> None:
    """Make sure `document.file_path` exists (where each saved file lives, relative to the upload folder) and fill it for
    rows that have none. Additive and idempotent: an existing column and its values are never changed. A row is filled only
    when its file is really in that asset's folder; the stored path is relative, so moving the upload folder keeps it valid."""
    if not any(r[1] == 'file_path' for r in conn.execute('PRAGMA table_info(document)')):
        conn.execute('ALTER TABLE document ADD COLUMN file_path TEXT')
    todo = conn.execute('SELECT d.document_id, d.file_name, a.name AS asset_code FROM document d JOIN asset a ON a.asset_id = d.asset_id '
                        "WHERE d.file_path IS NULL OR d.file_path = ''").fetchall()
    for r in todo:
        folder = asset_folder(root, r['asset_code']).resolve()
        target = (folder / r['file_name']).resolve()
        if target.parent == folder and target.is_file():
            conn.execute('UPDATE document SET file_path = ? WHERE document_id = ?', (target.relative_to(root).as_posix(), r['document_id']))
    conn.commit()


def safe_name(name: str | None) -> tuple[str, str]:
    """(file name, lower-case suffix). Only the last path part is kept and odd characters become '_'."""
    base = re.split(r'[\\/]', name or '')[-1].strip().strip('.')
    cleaned = _SAFE_NAME.sub('_', base)[:150]
    suffix = Path(cleaned).suffix.lower()
    if not cleaned or not suffix:
        raise _bad('The file needs a name and an extension.')
    return cleaned, suffix


def add_document(conn: sqlite3.Connection, user: CurrentUser, asset: sqlite3.Row, type_code: str,
                 file_name: str | None, stream: BinaryIO) -> dict:
    root = _root()
    kind = conn.execute('SELECT code, accept FROM file_type WHERE code = ?', (type_code,)).fetchone() if isinstance(type_code, str) else None
    if kind is None:
        raise _bad('Choose one of the document types.')
    name, suffix = safe_name(file_name)
    try:
        allowed = [str(x).lower() for x in json.loads(kind['accept'] or '[]')]
    except ValueError:
        allowed = []
    if suffix not in allowed:
        raise _bad(f"{kind['code']} accepts {', '.join(allowed) or 'no'} files.")

    limit = get_settings().max_upload_bytes
    folder = asset_folder(root, asset['name'])
    target = (folder / name).resolve()
    if folder.resolve() != target.parent or root not in target.parents:
        raise _bad('The file name is not valid.')  # never write outside the upload folder

    def work() -> int:
        if conn.execute('SELECT 1 FROM document_file_type t JOIN document d ON d.document_id = t.document_id '
                        'WHERE d.asset_id = ? AND t.position = 1 AND t.type_code = ?', (asset['asset_id'], type_code)).fetchone():
            raise HTTPException(status.HTTP_409_CONFLICT, 'This asset already has a file of this document type.')
        if conn.execute('SELECT 1 FROM document WHERE asset_id = ? AND lower(file_name) = lower(?)', (asset['asset_id'], name)).fetchone():
            raise HTTPException(status.HTTP_409_CONFLICT, 'A file with this name is already uploaded for this asset.')
        size = _save(stream, target, suffix, limit)
        try:
            su = repo.structx_user(conn, user)
            by = su['user_id'] if su else None
            now = _now()
            cur = conn.execute('INSERT INTO document (asset_id, file_name, size_bytes, suffix, macro_enabled, uploaded_by, uploaded_at) '
                               'VALUES (?,?,?,?,?,?,?)', (asset['asset_id'], name, size, suffix, int(suffix == '.xlsm'), by, now))
            doc_id = cur.lastrowid
            conn.execute('UPDATE document SET file_path = ? WHERE document_id = ?', (target.relative_to(root).as_posix(), doc_id))
            conn.execute('INSERT INTO document_file_type (document_id, type_code, position) VALUES (?,?,1)', (doc_id, type_code))
            conn.execute('INSERT INTO audit_log (user_id, asset_id, event, at) VALUES (?,?,?,?)',
                         (by, asset['asset_id'], f'uploaded {name}'[:200], now))
            return doc_id
        except Exception:
            target.unlink(missing_ok=True)  # the row was not saved, so the file must not stay
            raise

    ensure_path_column(conn, root)
    doc_id = _transaction(conn, work)
    return ui.ui_document(conn, doc_id)


def remove_document(conn: sqlite3.Connection, user: CurrentUser, doc_id: int) -> None:
    """DELETE /documents/{id}: removes the record and its type labels, then the file from the upload folder.

    Values already extracted from the file stay (their `document_id` is cleared, as the AI package does). The file is only
    deleted when its path, rebuilt from the configured folder and the stored name, is inside that folder."""
    doc = conn.execute('SELECT d.document_id, d.asset_id, d.file_name, a.name AS asset_code FROM document d '
                       'JOIN asset a ON a.asset_id = d.asset_id WHERE d.document_id = ?', (doc_id,)).fetchone()
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'Document not found.')
    repo.require_asset_code(conn, user, doc['asset_code'], write=True)  # object-level check: this asset, with edit rights

    def work() -> None:
        conn.execute('UPDATE input_value SET document_id = NULL WHERE document_id = ?', (doc_id,))
        conn.execute('DELETE FROM document_file_type WHERE document_id = ?', (doc_id,))
        conn.execute('DELETE FROM document WHERE document_id = ?', (doc_id,))
        su = repo.structx_user(conn, user)
        conn.execute('INSERT INTO audit_log (user_id, asset_id, event, at) VALUES (?,?,?,?)',
                     (su['user_id'] if su else None, doc['asset_id'], f"removed {doc['file_name']}"[:200], _now()))

    _transaction(conn, work)
    try:  # after the commit: a file that cannot be deleted must not undo the removal
        root = _root()
        folder = asset_folder(root, doc['asset_code']).resolve()
        target = (folder / doc['file_name']).resolve()
        if target.parent == folder and root in target.parents:
            target.unlink(missing_ok=True)
        else:
            log.error('a document file name pointed outside the upload folder; the file was not deleted')
    except (OSError, HTTPException):
        log.error('the removed document file could not be deleted')


def _save(stream: BinaryIO, target: Path, suffix: str, limit: int) -> int:
    """Write the upload to `target` (never over an existing file); returns its size. Removes a partial file on any error."""
    first = stream.read(16)
    if not first:
        raise _bad('The file is empty.')
    if not any(first.startswith(m) for m in _MAGIC.get(suffix, ())) and suffix in _MAGIC:
        raise _bad(f'The content is not a valid {suffix} file.')
    if suffix == '.csv' and b'\x00' in first:
        raise _bad('The content is not a valid .csv file.')
    target.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    try:
        with open(target, 'xb') as out:
            chunk = first
            while chunk:
                size += len(chunk)
                if size > limit:
                    raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, f'Files must be {limit // 1024 // 1024} MB or smaller.')
                out.write(chunk)
                chunk = stream.read(_CHUNK)
    except FileExistsError:
        raise HTTPException(status.HTTP_409_CONFLICT, 'A file with this name is already stored for this asset.') from None
    except HTTPException:
        target.unlink(missing_ok=True)
        raise
    except OSError:
        log.exception('document file could not be stored')
        target.unlink(missing_ok=True)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, 'The file could not be stored. Please try again.') from None
    return size
