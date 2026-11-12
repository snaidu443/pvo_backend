"""Creates the API's SQLite database: its five asset tables, empty (asset,
indication, cdp, ci, protocol, generated from specs/*.json), the user
tables (roles seeded) and the input tables seeded from specs/inputs.json, at the path STRUCTX_DATABASE_URL names in .env (default
sqlite:///db/structx.sqlite, relative to the project folder). Run from the
project folder:

    python db\\init_db.py            # create if missing, verify the schema if present
    python db\\init_db.py --fresh    # delete and recreate (every asset's rows are lost)
    python db\\init_db.py --seed-demo # also insert the mock's six synthetic demo users (empty user table only)

The server (api/main.py) does the same on start, so this is a convenience
for creating the file ahead of time or resetting it. An existing database
whose tables lack a column of the current specs is reported and left
alone unless --fresh is given.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.settings import sqlite_path  # noqa: E402
from api.store import Store  # noqa: E402
from structx.settings.config import ConfigError  # noqa: E402
from structx.join.users import seed_demo_users  # noqa: E402
from structx.settings.spec import load_spec  # noqa: E402

DEFAULT_URL = "sqlite:///db/structx.sqlite"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create (or reset) the API's SQLite database with empty tables.")
    parser.add_argument("--seed-demo", action="store_true", help="insert the mock's six synthetic demo users when the user table is empty")
    parser.add_argument("--fresh", action="store_true", help="delete an existing database first")
    args = parser.parse_args(argv)
    try:
        path = sqlite_path(os.environ.get("STRUCTX_DATABASE_URL") or DEFAULT_URL)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    if args.fresh:
        for side in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
            if side.exists():
                side.unlink()
    existed = path.exists()
    try:
        store = Store(path, {name: load_spec(name) for name in ("cdp", "ci", "protocol")})
    except ConfigError as exc:
        print(f"{path}: {exc}", file=sys.stderr)
        return 1
    try:
        if args.seed_demo:
            n = seed_demo_users(store.conn)
            store.conn.commit()
            print(f"demo users: {n} inserted")
        tables = [r[0] for r in store.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY rowid")]
        counts = {t: store.conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}
    finally:
        store.close()
    print(f"{'verified' if existed else 'created'} {path}: " + ", ".join(f"{t} ({n} rows)" for t, n in counts.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
