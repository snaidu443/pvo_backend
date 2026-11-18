"""Create the tables and load the React app's synthetic mock data (src/mocks/data) into the database.

    python -m scripts.seed --mocks ../Global_Policy_Re/src/mocks/data --admin-email you@company.com --admin-name "Your Name"

Safe to run again: existing rows are kept. Refuses to run against prod (demo data must never go there).
The --admin-email user becomes Admin and is linked to their Okta account on first sign-in.
"""
import argparse
import json
import re
from pathlib import Path

from sqlalchemy import func, select

from app import models
from app.config import get_settings
from app.database import Base, SessionLocal, engine
from app.schemas import AssetIn, UserIn


def load(folder: Path, name: str):
    return json.loads((folder / f'{name}.json').read_text(encoding='utf-8'))


def reference_data(folder: Path) -> dict:
    """Same composition as `referenceData` in src/api/mock/mockDb.ts."""
    return {
        'policies': load(folder, 'policies'),
        'recommendations': load(folder, 'recommendations'),
        'tour': load(folder, 'tour'),
        **load(folder, 'reference'),
        'copilot': load(folder, 'copilot'),
        'policyRules': load(folder, 'policyRules'),
        'iraConsiderations': load(folder, 'iraConsiderations'),
        'recommendationCards': load(folder, 'recommendationCards'),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--mocks', required=True, type=Path, help='path to Global_Policy_Re/src/mocks/data')
    parser.add_argument('--admin-email', help='your company email (becomes Admin)')
    parser.add_argument('--admin-name', default='PVO Admin')
    args = parser.parse_args()

    if get_settings().environment == 'prod':
        raise SystemExit('Refusing to load demo data into prod.')
    folder = args.mocks.resolve()
    if not (folder / 'assets.json').is_file():
        raise SystemExit(f'No assets.json in {folder}')

    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        catalogs = {'reference': reference_data(folder), 'policyVersions': load(folder, 'policyVersions'),
                    'policyDeltas': load(folder, 'policyDeltas'), 'rules': load(folder, 'rules'),
                    'policyMonitoring': load(folder, 'policyMonitoring')}
        for key, data in catalogs.items():  # catalogues are replaced: they are read-only and owned by DE
            row = db.get(models.Catalog, key)
            if row is None:
                db.add(models.Catalog(key=key, data=data))
            else:
                row.data = data

        for raw in load(folder, 'assets'):
            a = AssetIn.model_validate(raw)  # same validation as the API
            if db.get(models.Asset, a.id) is None:
                db.add(models.Asset(id=a.id, asset_code=a.assetCode, data=a.model_dump(exclude_none=True)))
        db.flush()

        for raw in load(folder, 'users'):
            u = UserIn.model_validate(raw)
            if db.get(models.User, u.id) is not None or db.scalar(select(models.User.id).where(models.User.email == u.email)):
                continue
            db.add(models.User(id=u.id, name=u.name, email=u.email, role=u.role, status=u.status,
                               data={'last': u.last, 'asset': u.asset, 'roleCode': u.roleCode, 'permissions': u.permissions or []}))
            db.flush()
            for asset_id in sorted(set(u.assetIds)):
                if db.get(models.Asset, asset_id) is not None:
                    db.add(models.AssetAccess(user_id=u.id, asset_id=asset_id))

        for asset_id, trace in load(folder, 'decisionTraces').items():
            if db.get(models.Asset, asset_id) is not None and db.get(models.DecisionTrace, asset_id) is None:
                db.add(models.DecisionTrace(asset_id=asset_id, data=trace))

        if args.admin_email:
            email = args.admin_email.strip().lower()
            if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email):
                raise SystemExit('--admin-email must be an email address, e.g. admin@example.test')
            admin = db.scalar(select(models.User).where(models.User.email == email))
            if admin is None:
                next_id = (db.scalar(select(func.max(models.User.id))) or 0) + 1
                db.add(models.User(id=next_id, name=args.admin_name[:60], email=email, role='Admin', status='active',
                                   data={'last': '', 'asset': 'All assets', 'roleCode': 'super_admin', 'permissions': []}))
            else:
                admin.role, admin.status = 'Admin', 'active'
        db.commit()
    print('Seed complete.')


if __name__ == '__main__':
    main()
