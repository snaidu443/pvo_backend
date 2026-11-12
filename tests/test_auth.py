import time

from conftest import OTHER_KEY, auth, make_token

ADMIN, EDITOR = 'admin@example.test', 'editor@example.test'


def test_health_needs_no_token(client):
    assert client.get('/health/live').json() == {'status': 'ok'}
    assert client.get('/health/ready').status_code == 200


def test_missing_token_is_401(client):
    assert client.get('/api/auth/me').status_code == 401


def test_me_returns_session_user(client):
    r = client.get('/api/auth/me', headers=auth(EDITOR))
    assert r.status_code == 200
    assert r.json() == {'name': 'Demo Editor', 'role': 'Editor'}


def test_bad_tokens_are_rejected(client):
    now = int(time.time())
    cases = [
        auth(ADMIN, aud='api://someone-else'),
        auth(ADMIN, iss='https://evil.example.test'),
        auth(ADMIN, exp=now - 3600, iat=now - 7200),
        auth(ADMIN, scp=['openid']),  # missing pvo.api scope
        {'Authorization': f'Bearer {make_token(ADMIN, key=OTHER_KEY)}'},  # signed by someone else
        {'Authorization': 'Bearer not-a-jwt'},
    ]
    for headers in cases:
        assert client.get('/api/auth/me', headers=headers).status_code == 401


def test_unknown_and_inactive_users_are_403(client):
    assert client.get('/api/auth/me', headers=auth('stranger@example.test')).status_code == 403
    assert client.get('/api/auth/me', headers=auth('former@example.test')).status_code == 403


def test_pending_user_becomes_active_on_first_sign_in(client):
    assert client.get('/api/auth/me', headers=auth('invitee@example.test')).status_code == 200
    users = client.get('/api/users', headers=auth(ADMIN)).json()
    assert next(u for u in users if u['id'] == 4)['status'] == 'active'
