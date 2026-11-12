"""Browse the LOCAL SQLite database table by table (read-only).

    .\\venv\\Scripts\\python.exe -m scripts.show_tables                     every table: row count and columns
    .\\venv\\Scripts\\python.exe -m scripts.show_tables documents           first 20 rows of one table
    .\\venv\\Scripts\\python.exe -m scripts.show_tables documents 50        first 50 rows
    .\\venv\\Scripts\\python.exe -m scripts.show_tables documents 20 PVO-F02CDFB5   only rows of one asset

Long values (such as the JSON `data` column) are cut to 60 characters. Nothing is changed.
"""
import re
import sqlite3
import sys
from pathlib import Path

from sqlalchemy.engine import make_url

from app.config import get_settings

_ASSET_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$')
_WIDTH = 60


def _connect() -> tuple[sqlite3.Connection, str] | None:
    url = make_url(get_settings().database_url)
    if url.get_backend_name() != 'sqlite' or not url.database or url.database == ':memory:':
        print('Refused: this script reads a local SQLite file only.')
        return None
    path = Path(url.database).resolve()
    if not path.exists():
        print('No database file yet. Run scripts.seed first.')
        return None
    return sqlite3.connect(f'{path.as_uri()}?mode=ro', uri=True), path.name  # read-only


def _cell(value) -> str:
    text = 'NULL' if value is None else str(value).replace('\n', ' ')
    return text if len(text) <= _WIDTH else text[: _WIDTH - 1] + '…'


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'  # identifier quoting; the name is also checked against the table list


def main() -> int:
    opened = _connect()
    if opened is None:
        return 1
    con, db_name = opened
    tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    columns = {t: [r[1] for r in con.execute(f'PRAGMA table_info({_quote(t)})')] for t in tables}

    if len(sys.argv) == 1:
        print(f'Database: {db_name}\n')
        print(f'{"table":<20} {"rows":>6}  columns')
        for t in tables:
            n = con.execute(f'SELECT COUNT(*) FROM {_quote(t)}').fetchone()[0]
            print(f'{t:<20} {n:>6}  {", ".join(columns[t])}')
        print('\nShow one table:  python -m scripts.show_tables <table> [rows] [asset id]')
        return 0

    table = sys.argv[1]
    if table not in tables:  # allow-list: only real table names
        print(f'Unknown table. Choose one of: {", ".join(tables)}')
        return 1
    try:
        limit = int(sys.argv[2]) if len(sys.argv) > 2 else 20
    except ValueError:
        limit = 0
    if not 1 <= limit <= 500:
        print('Rows must be a number from 1 to 500.')
        return 1

    sql, args = f'SELECT * FROM {_quote(table)}', []
    if len(sys.argv) > 3:
        asset_id = sys.argv[3]
        if not _ASSET_ID.match(asset_id):
            print('Invalid asset id.')
            return 1
        if 'asset_id' in columns[table]:
            sql, args = sql + ' WHERE asset_id = ?', [asset_id]
        elif table == 'assets':
            sql, args = sql + ' WHERE id = ?', [asset_id]
        else:
            print(f'{table} has no asset column; showing all rows.')
    order = 'id' if 'id' in columns[table] else 'rowid'
    rows = con.execute(f'{sql} ORDER BY {order} LIMIT ?', [*args, limit]).fetchall()
    total = con.execute(f'SELECT COUNT(*) FROM ({sql})', args).fetchone()[0]

    print(f'{table}: showing {len(rows)} of {total} row(s)\n')
    for i, row in enumerate(rows, 1):
        print(f'--- row {i}')
        for name, value in zip(columns[table], row):
            print(f'  {name:<22} {_cell(value)}')
    con.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
