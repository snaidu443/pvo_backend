"""Create (or re-activate) the local Test Admin in structx.sqlite, so there is an Admin who can manage users there.

Local development only: it refuses to run unless PVO_ENVIRONMENT=local. It writes nothing unless --apply is given.
The user must also exist in the app's own database (scripts/seed.py --admin-email ...) because sign-in still checks that
database; the two are matched by e-mail.

    python -m scripts.seed_structx_admin --email admin@example.test                 (shows what it would do)
    python -m scripts.seed_structx_admin --email admin@example.test --apply         (does it)

Back up structx.sqlite before using --apply. No password is created: sign-in has none of its own (see app/devauth.py).
"""
import argparse
import sqlite3
import sys

from app.config import get_settings
from structx.join import users as U


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--email', required=True)
    p.add_argument('--name', default='Test Admin')
    p.add_argument('--apply', action='store_true', help='write the change (otherwise only show it)')
    args = p.parse_args()

    settings = get_settings()
    if settings.environment != 'local':
        sys.exit('Refused: this script is for PVO_ENVIRONMENT=local only.')
    path = settings.structx_db_path.strip()
    if not path:
        sys.exit('PVO_STRUCTX_DB_PATH is not set.')

    conn = sqlite3.connect(f'file:{path}?mode={"rw" if args.apply else "ro"}', uri=True, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    try:
        email = args.email.strip().lower()
        existing = conn.execute('SELECT user_id, role_code, status FROM "user" WHERE email = ?', (email,)).fetchone()
        if existing is None:
            print('Would create the user as an active Admin.' if not args.apply else 'Creating the user as an active Admin.')
        elif (existing['role_code'], existing['status']) == ('admin', 'active'):
            print('Nothing to do: the user is already an active Admin.')
            return
        else:
            print('Would make the existing user an active Admin.' if not args.apply else 'Making the existing user an active Admin.')
        if not args.apply:
            print('Nothing was written. Add --apply to do it.')
            return
        conn.execute('BEGIN IMMEDIATE')
        try:
            if existing is None:
                made = U.create_user(conn, name=args.name, email=email, role_code='admin', status='active')
                U.audit(conn, made['user_id'], None, f'user {made["user_id"]} created (local test admin)')
            else:
                U.update_user(conn, existing['user_id'], role_code='admin', status='active')
                U.audit(conn, existing['user_id'], None, f'user {existing["user_id"]} made active Admin (local test admin)')
            conn.execute('COMMIT')
        except Exception:
            conn.execute('ROLLBACK')
            raise
        print('Done.')
    finally:
        conn.close()


if __name__ == '__main__':
    main()
