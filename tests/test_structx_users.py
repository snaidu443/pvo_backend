"""/api/structx/users against a small synthetic SQLite file shaped like structx.sqlite (no real data)."""
import sqlite3

import pytest
from conftest import auth
from test_structx import DDL, NOW

from app.config import get_settings

ADMIN, EDITOR, VIEWER = auth('admin@example.test'), auth('editor@example.test'), auth('viewer@example.test')
URL = '/api/structx/users'


@pytest.fixture()
def db(client, tmp_path, monkeypatch):
    path = tmp_path / 'structx_users.sqlite'
    c = sqlite3.connect(path)
    c.executescript(DDL)
    c.executemany('INSERT INTO asset (asset_id, name, asset_name, stage) VALUES (?,?,?,?)', [(1, 'SYN-101', 'One', 'Phase 2'), (2, 'SYN-202', 'Two', 'Phase 1')])
    c.executemany('INSERT INTO role VALUES (?,?,?,?,?,?,?)', [('admin', 'Admin', 'a', 1, 1, 1, 1), ('editor', 'Editor', 'e', 1, 1, 0, 2), ('viewer', 'Viewer', 'v', 0, 0, 0, 3)])
    c.executemany('INSERT INTO "user" VALUES (?,?,?,?,?,NULL,?,NULL)',
                  [(1, 'Test Admin', 'admin@example.test', 'admin', 'active', NOW),
                   (2, 'Demo Editor', 'editor@example.test', 'editor', 'active', NOW),
                   (3, 'Demo Viewer', 'viewer@example.test', 'viewer', 'active', NOW),
                   (4, 'Other Viewer', 'other@example.test', 'viewer', 'active', NOW),
                   (5, 'Demo Invitee', 'invitee@example.test', 'viewer', 'pending', NOW)])
    c.executemany('INSERT INTO user_asset VALUES (?,?,NULL,?)', [(2, 1, NOW), (3, 1, NOW), (4, 2, NOW)])
    c.commit()
    c.close()
    monkeypatch.setattr(get_settings(), 'structx_db_path', str(path))
    return path


def rows(path, sql, *args):
    c = sqlite3.connect(path)
    try:
        return c.execute(sql, args).fetchall()
    finally:
        c.close()


def body(**over):
    return {'name': 'New Person', 'email': 'New.Person@Example.test', 'role': 'Viewer', 'status': 'pending', 'assetIds': ['SYN-101'], **over}


# ---------------- who sees whom

def test_admin_sees_everyone_editor_sees_shared_assets_viewer_sees_self(client, db):
    assert [u['id'] for u in client.get(URL, headers=ADMIN).json()] == [1, 2, 3, 4, 5]
    assert [u['id'] for u in client.get(URL, headers=EDITOR).json()] == [2, 3]  # shares SYN-101 with the viewer; not the other viewer
    assert [u['id'] for u in client.get(URL, headers=VIEWER).json()] == [3]


def test_the_react_user_shape(client, db):
    u = client.get(f'{URL}/2', headers=ADMIN).json()
    assert (u['name'], u['email'], u['role'], u['status'], u['active'], u['assetIds'], u['last']) == ('Demo Editor', 'editor@example.test', 'Editor', 'active', True, ['SYN-101'], 'Never')
    assert (u['permissions'], u['asset']) == (['edit', 'see_users'], 'SYN-101')


def test_a_user_outside_your_scope_looks_like_an_unknown_user(client, db):
    assert client.get(f'{URL}/4', headers=EDITOR).status_code == 404
    assert client.get(f'{URL}/2', headers=VIEWER).status_code == 404
    assert client.get(f'{URL}/999', headers=ADMIN).status_code == 404


def test_a_caller_without_a_structx_user_is_refused(client, db):
    c = sqlite3.connect(db)
    c.execute("UPDATE \"user\" SET status = 'inactive' WHERE user_id = 3")
    c.commit()
    c.close()
    assert client.get(URL, headers=VIEWER).status_code == 403


def test_roles_are_listed(client, db):
    roles = client.get(f'{URL}/roles', headers=VIEWER).json()
    assert [r['name'] for r in roles] == ['Admin', 'Editor', 'Viewer'] and roles[0]['canManageUsers'] is True


# ---------------- writing

def test_only_a_manager_may_write(client, db):
    assert client.post(URL, json=body(), headers=EDITOR).status_code == 403
    assert client.put(f'{URL}/3', json=body(), headers=EDITOR).status_code == 403
    assert client.patch(f'{URL}/3/status', json={'status': 'inactive'}, headers=EDITOR).status_code == 403
    assert client.put(f'{URL}/3/assets', json={'assetIds': []}, headers=VIEWER).status_code == 403
    assert client.post(f'{URL}/5/invite', headers=EDITOR).status_code == 403
    assert rows(db, 'SELECT COUNT(*) FROM "user"') == [(5,)]


def test_admin_adds_a_user_with_assets_and_an_audit_row(client, db):
    r = client.post(URL, json=body(), headers=ADMIN)
    assert r.status_code == 201
    u = r.json()
    assert (u['email'], u['role'], u['status'], u['assetIds']) == ('new.person@example.test', 'Viewer', 'pending', ['SYN-101'])  # e-mail lower-cased
    assert rows(db, 'SELECT asset_id, granted_by FROM user_asset WHERE user_id = ?', u['id']) == [(1, 1)]
    assert rows(db, 'SELECT created_by FROM "user" WHERE user_id = ?', u['id']) == [(1,)]
    events = [e[0] for e in rows(db, 'SELECT event FROM audit_log')]
    assert events == [f'user {u["id"]} created']
    assert 'example.test' not in ' '.join(events)  # ids only, never an e-mail


@pytest.mark.parametrize('over, code', [({'email': 'editor@example.test'}, 409), ({'email': 'not-an-email'}, 422), ({'assetIds': ['NOPE']}, 422),
                                        ({'role': 'Owner'}, 422), ({'status': 'banned'}, 422), ({'name': ' '}, 422), ({'extra': 1}, 422)])
def test_bad_input_is_refused_and_nothing_is_saved(client, db, over, code):
    assert client.post(URL, json=body(**over), headers=ADMIN).status_code == code
    assert rows(db, 'SELECT COUNT(*) FROM "user"') == [(5,)]
    assert rows(db, 'SELECT COUNT(*) FROM audit_log') == [(0,)]


def test_admin_changes_a_user_and_the_assets(client, db):
    r = client.put(f'{URL}/3', json=body(name='Renamed', email='viewer@example.test', role='Editor', status='active', assetIds=['SYN-202']), headers=ADMIN)
    assert r.status_code == 200 and (r.json()['name'], r.json()['role'], r.json()['assetIds']) == ('Renamed', 'Editor', ['SYN-202'])
    assert rows(db, 'SELECT role_code FROM "user" WHERE user_id = 3') == [('editor',)]
    assert rows(db, 'SELECT asset_id FROM user_asset WHERE user_id = 3') == [(2,)]
    r = client.put(f'{URL}/3/assets', json={'assetIds': []}, headers=ADMIN)
    assert r.json()['assetIds'] == [] and rows(db, 'SELECT COUNT(*) FROM user_asset WHERE user_id = 3') == [(0,)]


def test_an_admin_cannot_change_their_own_role_or_status(client, db):
    assert client.put(f'{URL}/1', json=body(email='admin@example.test', role='Viewer', status='active'), headers=ADMIN).status_code == 400
    assert client.patch(f'{URL}/1/status', json={'status': 'inactive'}, headers=ADMIN).status_code == 400
    assert rows(db, 'SELECT role_code, status FROM "user" WHERE user_id = 1') == [('admin', 'active')]
    # keeping the same role and status while editing the name is fine
    assert client.put(f'{URL}/1', json=body(name='Test Admin 2', email='admin@example.test', role='Admin', status='active', assetIds=[]), headers=ADMIN).status_code == 200


def test_deactivate_and_activate(client, db):
    assert client.patch(f'{URL}/3/status', json={'status': 'inactive'}, headers=ADMIN).json()['active'] is False
    assert client.patch(f'{URL}/3/status', json={'status': 'active'}, headers=ADMIN).json()['active'] is True
    assert client.patch(f'{URL}/3/status', json={'status': 'pending'}, headers=ADMIN).status_code == 422  # only active / inactive here
    assert [e[0] for e in rows(db, 'SELECT event FROM audit_log ORDER BY log_id')] == ['user 3 deactivated', 'user 3 activated']


def test_resend_invite_only_for_a_pending_user(client, db):
    assert client.post(f'{URL}/5/invite', headers=ADMIN).status_code == 204
    assert client.post(f'{URL}/3/invite', headers=ADMIN).status_code == 409
    assert client.post(f'{URL}/999/invite', headers=ADMIN).status_code == 404


def test_users_cannot_be_deleted(client, db):
    assert client.delete(f'{URL}/3', headers=ADMIN).status_code == 405
