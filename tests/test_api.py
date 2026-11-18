from conftest import auth, legacy_assets

ADMIN, EDITOR, VIEWER = auth('admin@example.test'), auth('editor@example.test'), auth('viewer@example.test')


def ids(items):
    return sorted(x['id'] for x in items)


# ---------------- assets

def test_asset_list_never_falls_back_to_the_app_database(client):
    # /api/assets reads structx.sqlite only (see test_structx.py). Without it the route fails closed: it does not
    # quietly return the app's own demo assets.
    assert client.get('/api/assets', headers=ADMIN).status_code == 503
    assert ids(legacy_assets()) == ['DEMO-1', 'DEMO-2']  # still stored, just not served


# Creating, uploading and deleting assets / documents is no longer an API of this app (structx.sqlite is written by the AI team's flow).

# ---------------- users

def user(uid, email, role='Viewer', assets=('DEMO-1',), status='pending'):
    return {'id': uid, 'name': 'Demo Person', 'email': email, 'role': role, 'active': status == 'active', 'status': status,
            'last': 'Invite sent', 'asset': ', '.join(assets), 'assetIds': list(assets)}


def test_editor_invite_rules(client):
    assert client.post('/api/users', json=user(10, 'new@example.test'), headers=EDITOR).status_code == 201
    assert client.post('/api/users', json=user(11, 'boss@example.test', role='Admin'), headers=EDITOR).status_code == 403
    assert client.post('/api/users', json=user(12, 'x@example.test', assets=('DEMO-2',)), headers=EDITOR).status_code == 403
    assert client.post('/api/users', json=user(13, 'NEW@example.test'), headers=ADMIN).status_code == 409  # email is case-insensitive
    assert client.post('/api/users', json=user(14, 'v@example.test'), headers=VIEWER).status_code == 403


def test_admin_cannot_lock_themselves_out(client):
    me = user(1, 'admin@example.test', role='Viewer', assets=(), status='active')
    assert client.put('/api/users/1', json=me, headers=ADMIN).status_code == 400
    assert client.delete('/api/users/1', headers=ADMIN).status_code == 400


# ---------------- read-only data and PolicyPilot

def test_monitoring_is_admin_only(client):
    assert len(client.get('/api/policy-monitoring', headers=ADMIN).json()['findings']) == 1
    assert client.get('/api/policy-monitoring', headers=EDITOR).json()['findings'] == []


def test_copilot_answers_from_knowledge_base(client):
    r = client.post('/api/copilot/ask', json={'question': 'what is ira?', 'assetId': None}, headers=VIEWER)
    assert r.json() == {'text': 'Demo answer.'}
    assert client.post('/api/copilot/ask', json={'question': 'x', 'assetId': 'DEMO-2'}, headers=VIEWER).status_code == 404
