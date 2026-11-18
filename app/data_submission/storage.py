"""Saving an extraction into structx.sqlite: one transaction, all or nothing.

Uses the AI package's own storage functions so the tables are filled exactly as the package defines them:
    load_cdp / load_joined   replace the asset's `indication`, `cdp`, `ci`, `protocol` rows
    copy_extracted           copy the extracted cells into `input_value` (the table the screens edit)
Then the documents are marked extracted and one audit row is written (event text only, never values).

A fresh extraction replaces the asset's earlier extracted values AND any values a user entered per indication
(the package's rule). `has_existing_work` lets the caller ask for confirmation before that happens.
"""
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from ..structx_write import _now, _transaction

_REQUIRED_TABLES = {'asset', 'indication', 'cdp', 'ci', 'protocol', 'input', 'input_value', 'document', 'document_file_type', 'audit_log'}


@dataclass
class Outcome:
    """What the AI steps produced, already validated."""
    names: list[str]
    cdp_output: dict | None = None      # None when the CDP was not re-run (the stored indications were used)
    combined: dict | None = None        # the CI / protocol results joined to the indications
    ran: list[str] = field(default_factory=list)   # document types extracted: 'CDP', 'CI', 'Protocol'
    aliases: dict[str, Any] = field(default_factory=dict)
    cdp_saved: bool = False             # the indications and CDP values are already in the database (see save_cdp)


def schema_problem(conn: sqlite3.Connection) -> str | None:
    """None when the database can take the package's writes; otherwise a message safe to show the user."""
    have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    missing = sorted(_REQUIRED_TABLES - have)
    if missing:
        return f"The data source lacks the tables: {', '.join(missing)}."
    older = ('The data source is older than the extraction package ({}). '
             'Ask the AI team for the matching structx.sqlite.')
    if 'user_id' not in {r[1] for r in conn.execute('PRAGMA table_info(input_value)')}:
        return older.format('input_value has no user_id column')
    if 'geography' not in {r[1] for r in conn.execute('PRAGMA table_info(input)')}:  # the launch scope: which inputs an asset's launch market needs
        return older.format('the input catalogue has no geography column')
    return None


def has_existing_work(conn: sqlite3.Connection, asset: sqlite3.Row) -> bool:
    """True when the asset already has extracted data or per-indication values: saving would replace them."""
    if asset['extracted_at']:
        return True
    return conn.execute('SELECT 1 FROM input_value WHERE asset_id = ? AND indication_id IS NOT NULL LIMIT 1', (asset['asset_id'],)).fetchone() is not None


def save(conn: sqlite3.Connection, asset: sqlite3.Row, user_id: int | None, docs: dict[str, dict], out: Outcome) -> None:
    """`docs` = {'CDP': {'document_id', 'file_name'}, ...} for the types that were extracted. `conn` is a read-write
    connection with isolation_level=None. Raises on any failure after rolling back, so nothing is half-saved."""
    # Imported here: the package pulls in the OpenAI client, which is needed only when an extraction runs.
    from structx.join import database as D
    from structx.join import inputs as I
    from structx.settings.spec import load_spec

    specs = {name: load_spec(name) for name in ('cdp', 'ci', 'protocol')}
    asset_id, name = asset['asset_id'], asset['name']
    files = {kind.lower(): (docs[kind]['file_name'] if kind in docs else None) for kind in ('CDP', 'CI', 'Protocol')}

    do_cdp = out.cdp_output is not None and not out.cdp_saved   # the CDP part may have been saved already by save_cdp
    ran = [k for k in out.ran if not (k == 'CDP' and out.cdp_saved)]

    def work() -> None:
        if do_cdp:
            _write_cdp(conn, asset_id, name, docs, out, specs, files)
        for kind, spec_name in (('CI', 'ci'), ('Protocol', 'protocol')):
            if kind in out.ran and out.combined is not None:
                combined = _known_indications(conn, asset_id, out.combined) if out.cdp_saved else out.combined
                D.load_joined(conn, combined, spec_name, specs[spec_name], asset=name)
                I.copy_extracted(conn, asset_id, spec_name, document_id=docs[kind]['document_id'])
        now = _now()
        for kind in ran:
            conn.execute('UPDATE document SET extracted_at = ? WHERE document_id = ?', (now, docs[kind]['document_id']))
        conn.execute('UPDATE asset SET extracted_at = ?, updated_at = ? WHERE asset_id = ?', (now, now, asset_id))
        conn.execute('INSERT INTO audit_log (user_id, asset_id, event, at) VALUES (?,?,?,?)',
                     (user_id, asset_id, f"ran AI extraction ({', '.join(out.ran)})"[:200], now))

    _transaction(conn, work)


def _write_cdp(conn, asset_id, name, docs, out, specs, files) -> None:
    from structx.join import database as D
    from structx.join import inputs as I
    # load_cdp deletes the asset's indications before the per-indication values that point to them, which a database
    # with foreign keys on refuses. It deletes exactly these values anyway, so remove them first.
    conn.execute('DELETE FROM input_value WHERE asset_id = ? AND indication_id IS NOT NULL', (asset_id,))
    D.load_cdp(conn, out.cdp_output, specs['cdp'], asset=name, files=files, aliases=out.aliases)
    I.copy_extracted(conn, asset_id, 'cdp', document_id=docs['CDP']['document_id'])


def _known_indications(conn: sqlite3.Connection, asset_id: int, combined: dict) -> dict:
    """The CI / Protocol results are saved some minutes after the indications became editable. Drop the results of an
    indication the user renamed or deleted in the meantime (the package refuses an unknown name)."""
    from structx.join.combine import text_of
    have = {r[0] for r in conn.execute('SELECT indication FROM indication WHERE asset_id = ?', (asset_id,))}
    return {**combined, 'indications': [e for e in combined['indications'] if text_of(e['indication']['value']).strip() in have]}


def save_cdp(conn: sqlite3.Connection, asset: sqlite3.Row, user_id: int | None, docs: dict[str, dict], out: Outcome) -> None:
    """First part of a run: save the indications and the CDP values at once, so the user can open and edit the indications
    while the CI and Protocol are still being read. One transaction. Raises on failure after rolling back."""
    from structx.settings.spec import load_spec
    specs = {name: load_spec(name) for name in ('cdp', 'ci', 'protocol')}
    asset_id, name = asset['asset_id'], asset['name']
    files = {kind.lower(): (docs[kind]['file_name'] if kind in docs else None) for kind in ('CDP', 'CI', 'Protocol')}

    def work() -> None:
        _write_cdp(conn, asset_id, name, docs, out, specs, files)
        now = _now()
        conn.execute('UPDATE document SET extracted_at = ? WHERE document_id = ?', (now, docs['CDP']['document_id']))
        conn.execute('UPDATE asset SET extracted_at = ?, updated_at = ? WHERE asset_id = ?', (now, now, asset_id))

    _transaction(conn, work)
    out.cdp_saved = True
