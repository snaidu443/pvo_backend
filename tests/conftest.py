"""Test setup: in-memory SQLite, a locally generated RSA key standing in for Okta, and synthetic users.

Tokens are signed and checked with the real TokenVerifier; only the key source is replaced.
"""
import os
import secrets
import time

os.environ.update({  # before the app is imported (settings are read once)
    'PVO_ENVIRONMENT': 'test',
    'PVO_DATABASE_URL': 'sqlite://',
    'PVO_OKTA_ISSUER': 'https://okta.example.test/oauth2/default',
    'PVO_OKTA_AUDIENCE': 'api://pvo-test',
    'PVO_OKTA_CLIENT_ID': '',
    'PVO_DEV_LOGIN': 'false',
    'PVO_DEV_PASSWORD': secrets.token_urlsafe(16),  # random per run; no password is stored in the tests
})

import jwt  # noqa: E402
import pytest  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, event  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app import models  # noqa: E402
from app.database import Base, get_db, get_session_factory  # noqa: E402
from app.main import app  # noqa: E402
from app.security import TokenVerifier, get_verifier  # noqa: E402
from app.storage import LocalFileStorage, get_storage  # noqa: E402

ISSUER, AUDIENCE = os.environ['PVO_OKTA_ISSUER'], os.environ['PVO_OKTA_AUDIENCE']
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def make_token(sub: str, *, key=KEY, **overrides) -> str:
    now = int(time.time())
    claims = {'iss': ISSUER, 'aud': AUDIENCE, 'sub': sub, 'uid': f'okta-{sub}', 'iat': now, 'exp': now + 600, 'scp': ['openid', 'pvo.api']}
    claims.update(overrides)
    return jwt.encode(claims, key, algorithm='RS256')


def auth(sub: str, **overrides) -> dict:
    return {'Authorization': f'Bearer {make_token(sub, **overrides)}'}


def asset_json(asset_id: str, code: str | None = None, **extra) -> dict:
    """A minimal valid Asset (synthetic)."""
    return {
        'id': asset_id, 'assetCode': code or asset_id, 'assetName': f'{asset_id} demo', 'therapeuticArea': 'Oncology', 'stage': 'Phase 3',
        'jurisdictions': ['US'], 'policyExposures': ['IRA'], 'submissionStatus': 'Complete', 'submissionCompleteness': 100,
        'eligibilityStatus': '', 'businessContextStatus': '', 'impactStatus': '', 'owner': 'Demo Admin', 'lastUpdatedAt': '04 Nov 2026',
        'estimatedImpactLevel': '', 'estimatedAtRisk': 0, 'estimatedOpportunity': 0, 'recommendationStatus': '', 'governanceAlerts': 0,
        'summaryMetrics': {'gross': 0, 'net': 0, 'usRevenue': 0, 'euRevenue': 0}, 'eligibility': {}, 'context': {},
        'policyImpact': {'IRA': 0, 'MFN': 0, 'JCA': 0, 'EU-GPL': 0}, 'recommendationCount': 0, 'approvalCount': 0, **extra,
    }


@pytest.fixture()
def client(tmp_path):
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    event.listen(engine, 'connect', lambda conn, _: conn.execute('PRAGMA foreign_keys=ON'))
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    with Session() as db:  # synthetic users: admin, editor and viewer on DEMO-1; nobody but admin on DEMO-2
        db.add_all([models.User(id=1, name='Demo Admin', email='admin@example.test', role='Admin', status='active', data={}),
                    models.User(id=2, name='Demo Editor', email='editor@example.test', role='Editor', status='active', data={}),
                    models.User(id=3, name='Demo Viewer', email='viewer@example.test', role='Viewer', status='active', data={}),
                    models.User(id=4, name='Demo Invitee', email='invitee@example.test', role='Viewer', status='pending', data={}),
                    models.User(id=5, name='Demo Former', email='former@example.test', role='Editor', status='inactive', data={})])
        db.add_all([models.Asset(id='DEMO-1', asset_code='DEMO-1', data=asset_json('DEMO-1')),
                    models.Asset(id='DEMO-2', asset_code='DEMO-2', data=asset_json('DEMO-2'))])
        db.flush()
        db.add_all([models.AssetAccess(user_id=2, asset_id='DEMO-1'), models.AssetAccess(user_id=3, asset_id='DEMO-1')])
        db.add_all([models.Catalog(key='reference', data={'copilot': {'knowledgeBase': [{'question': 'What is IRA?', 'answer': 'Demo answer.'}]}}),
                    models.Catalog(key='policyMonitoring', data={'baselineDate': '15 Oct 2026', 'lastRefresh': '', 'pipelines': [],
                                                                  'documents': [], 'findings': [{'id': 'F1'}]})])
        db.commit()

    def _db():
        db = Session()
        try:
            yield db
        finally:
            db.rollback()
            db.close()

    public = KEY.public_key()
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_session_factory] = lambda: Session  # background extraction jobs
    app.dependency_overrides[get_verifier] = lambda: TokenVerifier(ISSUER, AUDIENCE, '', 'pvo.api', lambda _token: public)
    app.dependency_overrides[get_storage] = lambda: LocalFileStorage(tmp_path / 'uploads')
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
