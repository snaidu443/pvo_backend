"""Bring an existing LOCAL SQLite database up to date for Data Submission (no data is removed).

    venv\\Scripts\\python.exe -m scripts.upgrade_local_db

- Copies the database file to <name>.bak-<timestamp> first.
- Adds the new columns (assets.status, documents.doc_type ...) when they are missing.
- Creates the new table (extraction_jobs) and indexes.
Safe to run more than once. Refuses anything but a local SQLite file; real databases get proper migrations.
"""
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from sqlalchemy.engine import make_url

from app.config import get_settings

# Fixed DDL (no user input). Mirrors app/models.py.
NEW_COLUMNS = {
    'assets': [
        ('status', "VARCHAR(20) NOT NULL DEFAULT 'active'"),
    ],
    'documents': [
        ('doc_type', 'VARCHAR(20)'),
        ('seq', 'INTEGER'),
        ('original_name', 'VARCHAR(200)'),
        ('content_type', 'VARCHAR(120)'),
        ('storage_backend', "VARCHAR(10) NOT NULL DEFAULT 'local'"),
        ('storage_version_id', 'VARCHAR(200)'),
        ('sha256', 'VARCHAR(64)'),
        ('uploaded_by_user_id', 'INTEGER REFERENCES users(id) ON DELETE SET NULL'),
    ],
}
NEW_INDEXES = [
    'CREATE INDEX IF NOT EXISTS ix_assets_status ON assets (status)',
    'CREATE UNIQUE INDEX IF NOT EXISTS ux_documents_storage_key ON documents (storage_key)',
    'CREATE UNIQUE INDEX IF NOT EXISTS ux_documents_asset_sha256 ON documents (asset_id, sha256)',
]


def main() -> int:
    s = get_settings()
    if s.environment != 'local':
        print('Refused: this script is for local development only.')
        return 1
    url = make_url(s.database_url)
    if url.get_backend_name() != 'sqlite' or not url.database or url.database == ':memory:':
        print('Refused: PVO_DATABASE_URL is not a local SQLite file.')
        return 1
    path = Path(url.database).resolve()
    if not path.exists():
        print('No database file yet. Run scripts.seed instead; it creates every table.')
        return 1

    backup = path.with_name(f'{path.name}.bak-{datetime.now():%Y%m%d-%H%M%S}')
    shutil.copy2(path, backup)
    print(f'Backup: {backup.name}')

    added = []
    with sqlite3.connect(path) as conn:
        for table, columns in NEW_COLUMNS.items():
            have = {row[1] for row in conn.execute(f'PRAGMA table_info({table})')}  # table names are constants above
            for name, ddl in columns:
                if name not in have:
                    conn.execute(f'ALTER TABLE {table} ADD COLUMN {name} {ddl}')
                    added.append(f'{table}.{name}')
        for ddl in NEW_INDEXES:
            conn.execute(ddl)

    from app import models  # noqa: F401 - registers every table
    from app.database import Base, engine
    Base.metadata.create_all(engine)  # new tables only (extraction_jobs); existing tables are not touched

    print('Added columns: ' + (', '.join(added) if added else 'none (already up to date)'))
    print('Tables and indexes: up to date')
    return 0


if __name__ == '__main__':
    sys.exit(main())
