import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import devauth
from app.config import Settings
from app.database import get_db
from app.main import app as main_app
from app.routers import auth
from app.security import get_verifier

PASSWORD = os.environ['PVO_DEV_PASSWORD']  # random, set in conftest


def test_dev_login_refused_outside_local_or_without_password():
    for env in ('dev', 'test', 'prod'):
        with pytest.raises(RuntimeError):
            Settings(environment=env, dev_login=True, dev_password=PASSWORD,
                     okta_issuer='https://x.example.test', okta_audience='api://x').check()
    with pytest.raises(RuntimeError):
        Settings(environment='local', dev_login=True, dev_password='').check()
    Settings(environment='local', dev_login=True, dev_password=PASSWORD).check()  # allowed locally


def test_dev_token_round_trip():
    claims = devauth.dev_verifier().verify(devauth.issue_token('editor@example.test'))
    assert claims['sub'] == 'editor@example.test'


def test_dev_login_flow(client):
    """Mounts the dev routes on a small app that shares the test database, like PVO_DEV_LOGIN=true."""
    dev = FastAPI()
    dev.include_router(devauth.router, prefix='/api')
    dev.include_router(auth.router, prefix='/api')
    dev.dependency_overrides[get_db] = main_app.dependency_overrides[get_db]
    dev.dependency_overrides[get_verifier] = devauth.dev_verifier
    c = TestClient(dev)
    login = lambda username, password=PASSWORD: c.post('/api/auth/dev-login', json={'username': username, 'password': password})

    r = login('editor')  # user name = the part before @
    assert r.status_code == 200 and r.json()['user'] == {'name': 'Demo Editor', 'role': 'Editor'}
    me = c.get('/api/auth/me', headers={'Authorization': f"Bearer {r.json()['access_token']}"})
    assert me.json() == {'name': 'Demo Editor', 'role': 'Editor'}
    assert login('Admin@Example.test').status_code == 200  # full email, any case

    assert login('editor', 'wrong').status_code == 401
    assert login('former').status_code == 401  # inactive
    assert login('nobody').status_code == 401
    assert login('%').status_code == 401  # wildcards are not patterns
    assert c.post('/api/auth/dev-login', json={'email': 'editor@example.test'}).status_code == 422
