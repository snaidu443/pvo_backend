import json

from conftest import asset_json, auth

ADMIN, EDITOR, VIEWER = auth('admin@example.test'), auth('editor@example.test'), auth('viewer@example.test')


def ids(items):
    return sorted(x['id'] for x in items)


# ---------------- assets

def test_users_only_see_their_assets(client):
    assert ids(client.get('/api/assets', headers=ADMIN).json()) == ['DEMO-1', 'DEMO-2']
    assert ids(client.get('/api/assets', headers=EDITOR).json()) == ['DEMO-1']


def test_asset_save_rules(client):
    body = asset_json('DEMO-1', stage='Phase 2')
    assert client.put('/api/assets/DEMO-1', json=body, headers=VIEWER).status_code == 403
    assert client.put('/api/assets/DEMO-2', json=asset_json('DEMO-2'), headers=EDITOR).status_code == 404  # no access
    assert client.put('/api/assets/DEMO-1', json=asset_json('DEMO-2'), headers=EDITOR).status_code == 422  # id mismatch
    assert client.put('/api/assets/DEMO-1', json={**body, 'unexpected': 1}, headers=EDITOR).status_code == 422
    r = client.put('/api/assets/DEMO-1', json=body, headers=EDITOR)
    assert r.status_code == 200 and r.json()['stage'] == 'Phase 2'


def test_create_and_delete_added_asset(client):
    payload = {'asset': asset_json('NEW-001', 'DEMO-NEW'), 'fields': [], 'questions': [], 'trace': {'firedRules': []}}
    r = client.post('/api/assets', json=payload, headers=EDITOR)
    assert r.status_code == 201 and r.json()['createdByRole'] == 'Editor'
    assert 'NEW-001' in ids(client.get('/api/assets', headers=EDITOR).json())  # creator gets access
    assert client.post('/api/assets', json=payload, headers=EDITOR).status_code == 409
    assert client.delete('/api/assets/DEMO-1', headers=EDITOR).status_code == 403  # seeded assets stay
    assert client.delete('/api/assets/NEW-001', headers=EDITOR).status_code == 204
    assert 'NEW-001' not in ids(client.get('/api/assets', headers=ADMIN).json())


# ---------------- documents

def doc_meta(asset_id='DEMO-1', **extra):
    return json.dumps({'id': 123, 'assetId': asset_id, 'name': 'cdp.pdf', 'size': '0.1 MB', 'type': 'CDP', 'scope': 'Program-wide',
                       'version': 'Latest', 'tag': '', 'pages': 'Demo upload', 'added': 'Just now', **extra})


def test_document_upload(client):
    files = {'file': ('cdp.pdf', b'%PDF-1.4 synthetic', 'application/pdf')}
    r = client.post('/api/assets/DEMO-1/documents', data={'metadata': doc_meta()}, files=files, headers=EDITOR)
    assert r.status_code == 201
    doc = r.json()
    assert doc['id'] != 123 and doc['assetId'] == 'DEMO-1'  # the server assigns the id
    bad = {'file': ('run.exe', b'MZ', 'application/octet-stream')}
    assert client.post('/api/assets/DEMO-1/documents', data={'metadata': doc_meta()}, files=bad, headers=EDITOR).status_code == 422
    assert client.post('/api/assets/DEMO-1/documents', data={'metadata': doc_meta()}, files=files, headers=VIEWER).status_code == 403
    assert client.post('/api/assets/DEMO-2/documents', data={'metadata': doc_meta('DEMO-2')}, files=files, headers=EDITOR).status_code == 404
    assert client.delete(f"/api/documents/{doc['id']}", headers=EDITOR).status_code == 204


# ---------------- fields

def field(fid='DEMO-1|Indication 1 · Demo|US launch year', asset_id='DEMO-1', value='2029'):
    return {'id': fid, 'assetId': asset_id, 'group': 'Indication 1 · Demo', 'field': 'US launch year', 'value': value,
            'confidence': 'High', 'source': 'CDP · p.6', 'feeds': ['IRA'], 'status': 'AI-extracted'}


def test_fields_replace_and_save(client):
    assert client.put('/api/assets/DEMO-1/fields', json=[field()], headers=EDITOR).status_code == 200
    r = client.patch('/api/fields/DEMO-1%7CIndication%201%20%C2%B7%20Demo%7CUS%20launch%20year', json=field(value='2030'), headers=EDITOR)
    assert r.status_code == 200 and r.json()['value'] == '2030'
    assert client.put('/api/assets/DEMO-1/fields', json=[field(asset_id='DEMO-2')], headers=EDITOR).status_code == 422
    assert [f['value'] for f in client.get('/api/fields', headers=VIEWER).json()] == ['2030']


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
