"""Show what is stored for one asset in the LOCAL SQLite database (read-only).

    .\\venv\\Scripts\\python.exe -m scripts.show_asset PVO-F02CDFB5

Prints ids, types, sizes and statuses only; never file contents or storage paths.
"""
import re
import sqlite3
import sys
from pathlib import Path

from sqlalchemy.engine import make_url

from app.config import get_settings

_ASSET_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$')


def main() -> int:
    if len(sys.argv) != 2 or not _ASSET_ID.match(sys.argv[1]):
        print('Usage: python -m scripts.show_asset <asset id>   e.g. PVO-F02CDFB5')
        return 1
    asset_id = sys.argv[1]
    url = make_url(get_settings().database_url)
    if url.get_backend_name() != 'sqlite' or not url.database:
        print('Refused: this script reads a local SQLite file only.')
        return 1
    path = Path(url.database).resolve()
    if not path.exists():
        print(f'No database file at {path.name}.')
        return 1

    con = sqlite3.connect(f'{path.as_uri()}?mode=ro', uri=True)  # read-only
    q = lambda sql, *args: con.execute(sql, args).fetchall()  # noqa: E731 - parameterised queries only

    asset = q('SELECT id, asset_code, status, created_at FROM assets WHERE id = ?', asset_id)
    if not asset:
        print(f'No asset {asset_id} in {path.name}.')
        return 1
    print(f'Database: {path.name}\n')
    print('assets:            id, code, status, created_at')
    print('                  ', asset[0])
    print('\nuser_asset_access: user_id')
    for row in q('SELECT user_id FROM user_asset_access WHERE asset_id = ?', asset_id):
        print('                  ', row[0])
    print('\ndocuments:         id, doc_type, seq, size_bytes, scan_status, storage_backend, has_file')
    for row in q('SELECT id, doc_type, seq, size_bytes, scan_status, storage_backend, storage_key IS NOT NULL '
                 'FROM documents WHERE asset_id = ? ORDER BY doc_type, seq, id', asset_id):
        print('                  ', row)
    print('\nextraction_jobs:   id, status, error, fields_found')
    for row in q('SELECT id, status, error, fields_found FROM extraction_jobs WHERE asset_id = ? ORDER BY id', asset_id):
        print('                  ', row)
    print('\naudit_log:         action, created_at')
    for row in q("SELECT action, created_at FROM audit_log WHERE target LIKE ? ESCAPE '\\' ORDER BY id",
                 f'%asset:{asset_id.replace("_", chr(92) + "_")}%'):
        print('                  ', row)
    con.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
