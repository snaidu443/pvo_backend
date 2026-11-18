"""The documents a user attaches to an asset on the Data Submission page
and the four file types of the mock's upload slots (the `x-file` card's
type multiselect: CI, Protocol, CDP, Forecast). A document record is the
file's name, size, suffix and types (at least one; position 1 is the
primary type, the slot it is shown in); the bytes are never kept (they go
to the extraction and the temp copy is deleted). Adding a document whose
primary type is already held by another document replaces that document
(one file per slot, as the mock). `.xlsm` files are flagged and never
opened. Pure code.

    file_type           code, label, example, formats, accept (JSON list of suffixes), position, spec (cdp | ci | protocol | null)
    document            document_id, asset_id, file_name, size_bytes, suffix, macro_enabled, uploaded_by, uploaded_at, extracted_at
    document_file_type  document_id, type_code, position
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import PurePosixPath

FILE_TYPE_SEED = (
    ("CI", "Competitive Intelligence", "CI_Quarterly_Update.pdf", "PDF, PowerPoint", [".pdf", ".ppt", ".pptx"], "ci"),
    ("Protocol", "Trial Protocol Concept Sheet", "Protocol_Concept_Sheet.docx", "PDF, Word", [".pdf", ".doc", ".docx"], "protocol"),
    ("CDP", "Clinical Development Plan (CDP)", "CDP_FY27-29.pptx", "PDF, Word, PowerPoint", [".pdf", ".doc", ".docx", ".ppt", ".pptx"], "cdp"),
    ("Forecast", "Forecast Assumptions", "Forecast_v3.xlsx or Forecast.pptx", "Excel or PowerPoint (format to be confirmed)",
     [".xls", ".xlsx", ".xlsm", ".csv", ".ppt", ".pptx"], None),
)
DOCUMENT_TABLES: dict[str, list[tuple[str, str]]] = {
    "file_type": [("code", "TEXT PRIMARY KEY"), ("label", "TEXT NOT NULL"), ("example", "TEXT NOT NULL"), ("formats", "TEXT NOT NULL"),
                  ("accept", "TEXT NOT NULL"), ("position", "INTEGER NOT NULL"), ("spec", "TEXT")],
    "document": [("document_id", "INTEGER PRIMARY KEY"), ("asset_id", "INTEGER NOT NULL REFERENCES asset(asset_id)"),
                 ("file_name", "TEXT NOT NULL"), ("size_bytes", "INTEGER NOT NULL"), ("suffix", "TEXT NOT NULL"), ("macro_enabled", "INTEGER NOT NULL"),
                 ("uploaded_by", "INTEGER REFERENCES user(user_id)"), ("uploaded_at", "TEXT NOT NULL"), ("extracted_at", "TEXT")],
    "document_file_type": [("document_id", "INTEGER NOT NULL REFERENCES document(document_id)"),
                           ("type_code", "TEXT NOT NULL REFERENCES file_type(code)"), ("position", "INTEGER NOT NULL"),
                           ("PRIMARY KEY (document_id, type_code)", "")],
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _rows(conn: sqlite3.Connection, sql: str, *args) -> list[dict]:
    cur = conn.execute(sql, args)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def seed_file_types(conn: sqlite3.Connection) -> None:
    for position, (code, label, example, formats, accept, spec) in enumerate(FILE_TYPE_SEED, start=1):
        conn.execute('INSERT INTO "file_type" (code, label, example, formats, accept, position, spec) VALUES (?, ?, ?, ?, ?, ?, ?) '
                     'ON CONFLICT(code) DO UPDATE SET label = excluded.label, example = excluded.example, formats = excluded.formats, '
                     'accept = excluded.accept, position = excluded.position, spec = excluded.spec',
                     (code, label, example, formats, json.dumps(accept), position, spec))


def file_types(conn: sqlite3.Connection) -> list[dict]:
    out = _rows(conn, 'SELECT * FROM "file_type" ORDER BY position')
    for t in out:
        t["accept"] = json.loads(t["accept"])
    return out


def check_types(conn: sqlite3.Connection, types: object, suffix: str) -> list[str]:
    """The type codes validated (non-empty, known, distinct) for a file with `suffix` (accepted by at least one of them)."""
    known = {t["code"]: t for t in file_types(conn)}
    if not isinstance(types, list) or not types or any(not isinstance(t, str) for t in types):
        raise ValueError("a document needs at least one label")
    if len(set(types)) != len(types) or any(t not in known for t in types):
        raise ValueError(f"labels must be distinct and among {', '.join(known)}")
    suffix = (suffix or "").lower()
    if not suffix or not any(suffix in known[t]["accept"] for t in types):
        raise ValueError(f"a {suffix or 'file without a suffix'} file is not accepted for {', '.join(types)}")
    return list(types)


def _document(conn: sqlite3.Connection, document_id: int) -> dict:
    row = _rows(conn, 'SELECT * FROM "document" WHERE document_id = ?', document_id)[0]
    row["types"] = [r[0] for r in conn.execute('SELECT type_code FROM "document_file_type" WHERE document_id = ? ORDER BY position', (document_id,))]
    row["primary"] = row["types"][0] if row["types"] else None
    return row


def list_documents(conn: sqlite3.Connection, asset_id: int) -> list[dict]:
    return [_document(conn, r[0]) for r in conn.execute('SELECT document_id FROM "document" WHERE asset_id = ? ORDER BY document_id', (asset_id,))]


def document_for_type(conn: sqlite3.Connection, asset_id: int, code: str) -> dict | None:
    """The asset's document holding `code` among its types (primary first), or None."""
    row = conn.execute('SELECT d.document_id FROM "document" d JOIN "document_file_type" t ON t.document_id = d.document_id '
                       'WHERE d.asset_id = ? AND t.type_code = ? ORDER BY t.position, d.document_id LIMIT 1', (asset_id, code)).fetchone()
    return _document(conn, row[0]) if row else None


def _write_types(conn: sqlite3.Connection, document_id: int, types: list[str]) -> None:
    conn.execute('DELETE FROM "document_file_type" WHERE document_id = ?', (document_id,))
    for position, code in enumerate(types, start=1):
        conn.execute('INSERT INTO "document_file_type" (document_id, type_code, position) VALUES (?, ?, ?)', (document_id, code, position))


def add_document(conn: sqlite3.Connection, asset_id: int, *, file_name: str, size_bytes: int, types: list[str], uploaded_by: int | None = None,
                 replace_primary: bool = True) -> dict:
    """Records a file (the bytes are not kept); with `replace_primary`, deletes the asset's document whose primary
    type is this one's primary type first. ValueError on bad types / suffix. Does not commit."""
    suffix = PurePosixPath(file_name).suffix.lower()
    codes = check_types(conn, types, suffix)
    if replace_primary:
        for d in list_documents(conn, asset_id):
            if d["primary"] == codes[0]:
                delete_document(conn, d["document_id"])
    document_id = conn.execute('INSERT INTO "document" (asset_id, file_name, size_bytes, suffix, macro_enabled, uploaded_by, uploaded_at) VALUES (?, ?, ?, ?, ?, ?, ?)',
                               (asset_id, file_name[:200], int(size_bytes), suffix, int(suffix == ".xlsm"), uploaded_by, _now())).lastrowid
    _write_types(conn, document_id, codes)
    return _document(conn, document_id)


def set_types(conn: sqlite3.Connection, document_id: int, types: object) -> dict:
    doc = _document(conn, document_id)
    _write_types(conn, document_id, check_types(conn, types, doc["suffix"]))
    return _document(conn, document_id)


def delete_document(conn: sqlite3.Connection, document_id: int) -> None:
    """Deletes the record and its types; input values keep their rows with document_id cleared."""
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "input_value" in tables:
        conn.execute('UPDATE "input_value" SET document_id = NULL WHERE document_id = ?', (document_id,))
    conn.execute('DELETE FROM "document_file_type" WHERE document_id = ?', (document_id,))
    conn.execute('DELETE FROM "document" WHERE document_id = ?', (document_id,))


def clear_extracted(conn: sqlite3.Connection, asset_id: int, type_codes: list[str]) -> None:
    """extracted_at back to NULL on the asset's documents whose types include any of `type_codes`."""
    if not type_codes:
        return
    marks = ",".join("?" * len(type_codes))
    conn.execute(f'UPDATE "document" SET extracted_at = NULL WHERE asset_id = ? AND document_id IN (SELECT document_id FROM "document_file_type" WHERE type_code IN ({marks}))', (asset_id, *type_codes))


def mark_extracted(conn: sqlite3.Connection, document_ids: list[int]) -> None:
    now = _now()
    for d in document_ids:
        conn.execute('UPDATE "document" SET extracted_at = ? WHERE document_id = ?', (now, d))
