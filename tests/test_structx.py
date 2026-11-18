"""/api/structx routes against a small synthetic SQLite file shaped like structx.sqlite (no real data)."""
import sqlite3

import pytest
from conftest import auth

from app.config import get_settings

ADMIN, EDITOR, VIEWER = auth('admin@example.test'), auth('editor@example.test'), auth('viewer@example.test')
INVITEE = auth('invitee@example.test')

DDL = """
CREATE TABLE asset (asset_id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, asset_name TEXT, stage TEXT,
  owner_user_id INTEGER, created_by INTEGER, created_at TEXT, updated_at TEXT, cdp_file TEXT, ci_file TEXT, protocol_file TEXT,
  model TEXT, extracted_at TEXT, seconds REAL, aliases TEXT);
CREATE TABLE indication (indication_id INTEGER PRIMARY KEY, asset_id INTEGER NOT NULL, indication TEXT NOT NULL,
  "include" INTEGER NOT NULL DEFAULT 1, source TEXT NOT NULL DEFAULT 'cdp', position INTEGER, confidence TEXT,
  confidence_reason TEXT, citations TEXT);
CREATE TABLE role (code TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL, can_edit INTEGER NOT NULL,
  can_see_users INTEGER NOT NULL, can_manage_users INTEGER NOT NULL, position INTEGER NOT NULL);
CREATE TABLE audit_log (log_id INTEGER PRIMARY KEY, user_id INTEGER, asset_id INTEGER, event TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE "user" (user_id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL UNIQUE, role_code TEXT NOT NULL,
  status TEXT NOT NULL, last_active_at TEXT, created_at TEXT NOT NULL, created_by INTEGER);
CREATE TABLE user_asset (user_id INTEGER NOT NULL, asset_id INTEGER NOT NULL, granted_by INTEGER, granted_at TEXT NOT NULL,
  PRIMARY KEY (user_id, asset_id));
CREATE TABLE file_type (code TEXT PRIMARY KEY, label TEXT NOT NULL, example TEXT NOT NULL, formats TEXT NOT NULL,
  accept TEXT NOT NULL, position INTEGER NOT NULL, spec TEXT);
CREATE TABLE document (document_id INTEGER PRIMARY KEY, asset_id INTEGER NOT NULL, file_name TEXT NOT NULL,
  size_bytes INTEGER NOT NULL, suffix TEXT NOT NULL, macro_enabled INTEGER NOT NULL, uploaded_by INTEGER,
  uploaded_at TEXT NOT NULL, extracted_at TEXT);
CREATE TABLE document_file_type (document_id INTEGER NOT NULL, type_code TEXT NOT NULL, position INTEGER NOT NULL,
  PRIMARY KEY (document_id, type_code));
CREATE TABLE input (input_id INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE, name TEXT NOT NULL, category TEXT, section TEXT NOT NULL,
  level TEXT NOT NULL, kind TEXT NOT NULL, source TEXT NOT NULL, screen TEXT NOT NULL, spec TEXT, field TEXT,
  required INTEGER NOT NULL, condition TEXT, expected TEXT NOT NULL, source_text TEXT NOT NULL, explanation TEXT, policies TEXT NOT NULL,
  unit TEXT, workbook_row INTEGER NOT NULL);
CREATE TABLE input_option (option_id INTEGER PRIMARY KEY, input_id INTEGER NOT NULL, position INTEGER NOT NULL, value TEXT NOT NULL);
CREATE TABLE input_value (value_id INTEGER PRIMARY KEY, asset_id INTEGER NOT NULL, input_id INTEGER NOT NULL,
  indication_id INTEGER, competitor TEXT, value TEXT NOT NULL, source TEXT NOT NULL, status TEXT NOT NULL, confidence TEXT,
  confidence_reason TEXT, citations TEXT, document_id INTEGER, updated_by INTEGER, updated_at TEXT NOT NULL);
"""
NOW = '2026-01-01T00:00:00+00:00'


def _input(c, input_id, id_, screen, section, level, kind, required, row):
    c.execute('INSERT INTO input (input_id, id, name, category, section, level, kind, source, screen, required, expected, source_text, policies, workbook_row) '
              'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
              (input_id, id_, id_.replace('_', ' ').title(), 'Regulatory', section, level, kind, 'cdp', screen, required, '', '', '["IRA"]', row))


@pytest.fixture()
def structx(client, tmp_path, monkeypatch):
    path = tmp_path / 'structx_test.sqlite'
    c = sqlite3.connect(path)
    c.executescript(DDL)
    c.executemany('INSERT INTO asset (asset_id, name, asset_name, stage, extracted_at) VALUES (?,?,?,?,?)',
                  [(1, 'SYN-101', 'Synthetic One', 'Phase 2', NOW), (2, 'SYN-202', None, 'Phase 1', None)])
    c.executemany('INSERT INTO role VALUES (?,?,?,?,0,0,?)', [('editor', 'Editor', 'e', 1, 2), ('viewer', 'Viewer', 'v', 0, 3)])
    c.executemany('INSERT INTO "user" VALUES (?,?,?,?,?,NULL,?,NULL)',
                  [(1, 'Demo Editor', 'Editor@Example.test', 'editor', 'active', NOW),
                   (2, 'Demo Viewer', 'viewer@example.test', 'viewer', 'active', NOW),
                   (3, 'Demo Invitee', 'invitee@example.test', 'viewer', 'pending', NOW)])
    c.executemany('INSERT INTO user_asset VALUES (?,?,NULL,?)', [(1, 1, NOW), (3, 1, NOW)])  # viewer has no grant
    c.executemany('INSERT INTO indication VALUES (?,?,?,?,?,?,?,?,?)',
                  [(1, 1, 'Indication A', 1, 'cdp', 1, 'high', 'clear', '{"citations":[{"page":3,"quote":"q"}]}'),
                   (2, 1, 'Indication B', 1, 'cdp', 2, 'low', None, None),
                   (3, 1, 'Indication C', 0, 'user', 3, None, None, None)])
    c.executemany('INSERT INTO file_type VALUES (?,?,?,?,?,?,?)',
                  [('CI', 'CI', 'e', 'f', '[".pdf", ".ppt", ".pptx"]', 2, 'ci'), ('Protocol', 'Protocol', 'e', 'f', '[".pdf", ".docx"]', 3, 'protocol'),
                   ('CDP', 'CDP', 'e', 'f', '[".pdf", ".pptx"]', 1, 'cdp'), ('Forecast', 'Forecast', 'e', 'f', '[".xlsx", ".xlsm", ".csv"]', 4, None)])
    c.executemany('INSERT INTO document VALUES (?,?,?,?,?,?,NULL,?,?)',
                  [(1, 1, 'deck.pptx', 10, '.pptx', 0, NOW, NOW), (2, 1, 'fc.xlsx', 5, '.xlsx', 0, NOW, None)])
    c.executemany('INSERT INTO document_file_type VALUES (?,?,1)', [(1, 'CDP'), (2, 'Forecast')])
    _input(c, 1, 'therapeutic_modality', 'submission', 'asset', 'asset', 'select', 1, 1)
    _input(c, 2, 'us_launch_timing', 'indications', 'asset', 'indication', 'year_month', 1, 2)
    _input(c, 3, 'clinical_trial_comparator_design', 'assumptions', 'asset', 'indication', 'select', 1, 3)
    _input(c, 4, 'competitor_phase_of_development', 'inputs', 'competitor', 'indication', 'select', 1, 4)
    _input(c, 5, 'competitor_expected_mfp_negotiation_timing', 'inputs', 'competitor', 'indication', 'calc', 1, 5)
    _input(c, 7, 'eu_launch_timing', 'indications', 'asset', 'indication', 'year', 0, 7)
    c.executemany('INSERT INTO input_option VALUES (?,?,?,?)', [(1, 3, 2, 'Placebo-controlled'), (2, 3, 1, 'Head-to-head'), (3, 1, 1, 'Biologic'), (4, 1, 2, 'Small molecule'),
                   (5, 4, 1, 'Phase II'), (6, 4, 2, 'Phase III'), (7, 4, 3, 'Launched'),
                   ])
    c.executemany('INSERT INTO input_value VALUES (?,?,?,?,?,?,?,?,?,?,?,?,NULL,?)',
                  [(1, 1, 1, None, None, '"Biologic"', 'user', 'entered', None, None, None, None, NOW),
                   (2, 1, 2, 1, None, '{"year": 2030, "month": 5}', 'cdp', 'ai_extracted', 'high', 'r', '{"citations":[]}', 1, NOW),
                   (3, 1, 4, 1, 'Comp X', '"Phase III"', 'ci', 'ai_extracted', 'medium', None, 'not json', 1, NOW),
                   (5, 1, 7, 1, None, '2031', 'cdp', 'ai_extracted', 'high', None, None, 1, NOW)])
    c.commit()
    c.close()
    monkeypatch.setattr(get_settings(), 'structx_db_path', str(path))
    return path


def test_not_configured_fails_closed(client, monkeypatch):
    monkeypatch.setattr(get_settings(), 'structx_db_path', '')
    assert client.get('/api/structx/assets', headers=ADMIN).status_code == 503
    monkeypatch.setattr(get_settings(), 'structx_db_path', 'does-not-exist.sqlite')
    assert client.get('/api/structx/assets', headers=ADMIN).status_code == 503


def test_requires_sign_in(client, structx):
    assert client.get('/api/structx/assets').status_code == 401


def test_asset_visibility_follows_structx_grants(client, structx):
    names = lambda h: [a['name'] for a in client.get('/api/structx/assets', headers=h).json()]  # noqa: E731
    assert names(ADMIN) == ['SYN-101', 'SYN-202']
    assert names(EDITOR) == ['SYN-101']  # matched by e-mail, case-insensitive
    assert names(VIEWER) == []  # exists in structx but has no grant
    assert names(INVITEE) == []  # not active in structx -> no access


def test_object_level_authorization(client, structx):
    for path in ('', '/indications', '/screens/inputs', '/coverage', '/extraction/status'):
        assert client.get(f'/api/structx/assets/2{path}', headers=EDITOR).status_code == 404  # no grant
        assert client.get(f'/api/structx/assets/999{path}', headers=ADMIN).status_code == 404  # unknown
        assert client.get(f'/api/structx/assets/1{path}', headers=VIEWER).status_code == 404
        assert client.get(f'/api/structx/assets/1{path}', headers=EDITOR).status_code == 200


def test_asset_and_documents(client, structx):
    body = client.get('/api/structx/assets/1', headers=EDITOR).json()
    assert body['assetName'] == 'Synthetic One' and body['stage'] == 'Phase 2'
    assert [(d['fileName'], d['primaryType'], d['extractionSpec']) for d in body['documents']] == [
        ('deck.pptx', 'CDP', 'cdp'), ('fc.xlsx', 'Forecast', None)]


def test_indications_screen(client, structx):
    body = client.get('/api/structx/assets/1/indications', headers=EDITOR).json()
    assert [(i['indication'], i['include'], i['source']) for i in body['indications']] == [
        ('Indication A', True, 'cdp'), ('Indication B', True, 'cdp'), ('Indication C', False, 'user')]
    assert body['indications'][0]['citations']['citations'][0]['page'] == 3
    item = body['items'][0]
    assert item['input']['id'] == 'us_launch_timing'
    assert item['values'][0]['value'] == {'year': 2030, 'month': 5} and item['values'][0]['status'] == 'ai_extracted'
    # Indication B (included) has no value; C is unticked so it is not expected
    assert item['missing'] is True and item['missingIndicationIds'] == [2]


def test_screens_keep_empty_inputs_and_parse_values(client, structx):
    sub = client.get('/api/structx/assets/1/screens/submission', headers=EDITOR).json()['items'][0]
    assert sub['values'][0]['value'] == 'Biologic' and sub['missing'] is False
    ass = client.get('/api/structx/assets/1/screens/assumptions', headers=EDITOR).json()['items'][0]
    assert ass['values'] == [] and ass['missing'] is True
    assert ass['input']['options'] == ['Head-to-head', 'Placebo-controlled']  # ordered by position
    inputs = {i['input']['id']: i for i in client.get('/api/structx/assets/1/screens/inputs', headers=EDITOR).json()['items']}
    comp = inputs['competitor_phase_of_development']
    assert comp['values'][0]['competitor'] == 'Comp X' and comp['values'][0]['citations'] == 'not json'  # bad JSON kept as text
    assert inputs['competitor_expected_mfp_negotiation_timing']['missing'] is False  # calc inputs are never stored


def test_unknown_screen_rejected(client, structx):
    assert client.get('/api/structx/assets/1/screens/nope', headers=ADMIN).status_code == 422
    assert client.get('/api/structx/assets/abc', headers=ADMIN).status_code == 422


def test_coverage_counts_required_inputs(client, structx):
    s = client.get('/api/structx/assets/1/coverage', headers=EDITOR).json()['screens']
    assert s['submission'] == {'required': 1, 'filled': 1, 'missing': []}
    assert s['indications']['missing'] == ['us_launch_timing']
    assert s['assumptions']['missing'] == ['clinical_trial_comparator_design']
    assert s['inputs'] == {'required': 1, 'filled': 1, 'missing': []}  # calc excluded


def test_extraction_status_is_imprecise_until_extraction_run_exists(client, structx):
    s = client.get('/api/structx/assets/1/extraction/status', headers=EDITOR).json()
    assert s['documents'] == [{'type': 'CDP', 'fileName': 'deck.pptx', 'spec': 'cdp', 'extractedAt': NOW, 'state': 'extracted'}]
    assert s['allExtracted'] is True and s['precise'] is False


def test_database_is_opened_read_only(client, structx):
    client.get('/api/structx/assets', headers=ADMIN)
    c = sqlite3.connect(structx)
    assert c.execute('SELECT COUNT(*) FROM input_value').fetchone()[0] == 4  # unchanged
    c.close()


# ---------------- GET /api/assets and /api/documents: the React app's shapes, built from structx.sqlite only

def test_assets_route_builds_the_ui_asset_from_structx(client, structx):
    c = sqlite3.connect(structx)  # one more AI-extracted protocol value, to check the catalogue mapping
    c.execute("INSERT INTO input_value VALUES (20,1,3,1,NULL,'\"Head-to-head\"','protocol','ai_extracted','high',NULL,NULL,1,NULL,?)", (NOW,))
    c.commit()
    c.close()
    assets = {a['id']: a for a in client.get('/api/assets', headers=ADMIN).json()}
    assert sorted(assets) == ['SYN-101', 'SYN-202']
    a = assets['SYN-101']
    assert (a['assetCode'], a['stage'], a['jurisdictions']) == ('SYN-101', 'Phase 2', ['US', 'EU']) and 'therapeuticArea' not in a
    assert a['workflowStatus'] == 'inputs_review' and a['submissionStatus'] == 'Needs review'
    assert a['assessments'][0]['date'] == '01 Jan 2026'  # supplied, so the app does not invent past assessments
    w = a['aia']
    assert w['created'] is True and w['unlocked'] == ['submission', 'governance']
    assert w['setup']['modality'] == 'Biologic' and 'area' not in w['setup'] and w['v6']['asset'] == {'modality': 'Biologic'}
    first, _second, unticked = w['setup']['indications']
    assert (first['id'], first['name'], first['launchUS'], first['launchUSMonth'], first['include'], first['source']) == (
        'IND-1', 'Indication A', '2030', 'May', True, 'CDP')
    assert first['v6'] == {'trialDesign': 'Head-to-head'} and first['launchEU'] == '2031'  # a year is text in the app
    assert unticked['include'] is False and unticked['source'] == 'Manual'
    assert w['v6']['comp'] == [{'name': 'Comp X', 'vsId': 'IND-1', 'cPhase': 'Phase III'}]
    # Nothing extracted yet and no documents: neutral defaults, nothing invented
    b = assets['SYN-202']
    assert (b['workflowStatus'], b['aia']['unlocked'], b['submissionCompleteness'], b['owner']) == ('active', ['submission'], 0, '—')
    assert 'therapeuticArea' not in b and b['estimatedAtRisk'] == 0 and b['eligibility']['IRA'] == 'Review pending' and b['aia']['setup']['indications'] == []


def test_assets_route_follows_structx_grants(client, structx):
    ids = lambda h: [a['id'] for a in client.get('/api/assets', headers=h).json()]  # noqa: E731
    assert ids(EDITOR) == ['SYN-101'] and ids(VIEWER) == [] and ids(INVITEE) == []
    assert client.get('/api/assets').status_code == 401


def test_documents_route_reads_structx(client, structx):
    docs = client.get('/api/documents', headers=EDITOR).json()
    assert [(d['assetId'], d['name'], d['type'], d['types']) for d in docs] == [
        ('SYN-101', 'deck.pptx', 'CDP', ['CDP']), ('SYN-101', 'fc.xlsx', 'Forecast', ['Forecast'])]
    assert 'scanStatus' not in docs[0]  # structx records no scan, so none is claimed
    assert client.get('/api/documents', headers=VIEWER).json() == []


def test_no_stored_fields_or_questions_are_served(client, structx):
    assert client.get('/api/fields', headers=EDITOR).json() == []
    assert client.get('/api/questions', headers=EDITOR).json() == []


# ---------------- saving: PUT /api/assets/{code} and PATCH /api/documents/{id} write structx.sqlite only

def rows(path, sql, *args):
    c = sqlite3.connect(path)
    try:
        return c.execute(sql, args).fetchall()
    finally:
        c.close()


def get_asset(client, code='SYN-101', headers=EDITOR):
    return next(a for a in client.get('/api/assets', headers=headers).json() if a['id'] == code)


def test_saving_an_unchanged_asset_changes_nothing(client, structx):
    before = (rows(structx, 'SELECT * FROM input_value ORDER BY value_id'), rows(structx, 'SELECT * FROM indication ORDER BY indication_id'))
    r = client.put('/api/assets/SYN-101', json=get_asset(client), headers=EDITOR)
    assert r.status_code == 200, r.text
    assert r.json()['id'] == 'SYN-101'
    assert (rows(structx, 'SELECT * FROM input_value ORDER BY value_id'), rows(structx, 'SELECT * FROM indication ORDER BY indication_id')) == before
    assert rows(structx, 'SELECT COUNT(*) FROM audit_log') == [(0,)]  # an AI value the user did not touch stays ai_extracted


def test_saving_edits_values_indications_and_details(client, structx):
    a = get_asset(client)
    w = a['aia']
    w['setup']['stage'] = 'Phase 3'
    first, second = w['setup']['indications'][:2]
    first['launchUS'], first['launchUSMonth'] = '2031', 'Nov'  # edits an AI value
    second['launchUS'], second['launchUSMonth'] = '2029', ''  # fills an empty cell
    first['v6']['trialDesign'] = 'Placebo-controlled'
    w['v6']['asset']['modality'] = 'Small molecule'
    w['v6']['comp'][0]['cPhase'] = 'Launched'
    w['setup']['indications'].append({'id': 'IND-9', 'name': 'Added by user', 'orphanUS': '', 'orphanEU': '', 'launchUS': '', 'launchUSMonth': '',
                                      'launchEU': '', 'include': True, 'source': 'Manual', 'v6': {'trialDesign': 'Head-to-head'}})
    r = client.put('/api/assets/SYN-101', json=a, headers=EDITOR)
    assert r.status_code == 200, r.text
    # the response is the asset as the app reads it back
    back = r.json()['aia']
    assert back['setup']['stage'] == 'Phase 3' and back['v6']['asset']['modality'] == 'Small molecule'
    assert [i['name'] for i in back['setup']['indications']][-1] == 'Added by user'

    by_key = {(i, ind, comp): (v, s, src, conf) for i, ind, comp, v, s, src, conf in rows(
        structx, "SELECT i.id, v.indication_id, v.competitor, v.value, v.status, v.source, v.confidence FROM input_value v JOIN input i USING (input_id)")}
    assert by_key[('us_launch_timing', 1, None)] == ('{"year":2031,"month":11}', 'edited', 'user', None)  # AI value -> edited
    assert by_key[('us_launch_timing', 2, None)] == ('{"year":2029,"month":null}', 'entered', 'user', None)
    assert by_key[('clinical_trial_comparator_design', 1, None)][1] == 'entered'  # the cell had no value
    assert by_key[('therapeutic_modality', None, None)] == ('"Small molecule"', 'entered', 'user', None)  # a user's own value stays "entered"
    assert by_key[('competitor_phase_of_development', 1, 'Comp X')][1] == 'edited'
    assert by_key[('clinical_trial_comparator_design', 9, None)][1] == 'entered'  # a value for the new indication
    assert rows(structx, 'SELECT indication, "include", source, position FROM indication WHERE indication_id = 9') == [('Added by user', 1, 'user', 4)]
    assert rows(structx, 'SELECT stage FROM asset WHERE asset_id = 1') == [('Phase 3',)]
    # the audit log says who changed what, never the values
    events = [e for (e,) in rows(structx, 'SELECT event FROM audit_log')]
    assert 'added indication' in events and 'set Clinical Trial Comparator Design' in events
    assert not any(x in ' '.join(events) for x in ('2031', 'Placebo', 'Small molecule', 'Launched'))
    assert {u for (u,) in rows(structx, 'SELECT DISTINCT user_id FROM audit_log')} == {1}  # the structx user matched by e-mail


def test_clearing_a_value_deletes_it_and_unticking_keeps_the_indication(client, structx):
    a = get_asset(client)
    a['aia']['v6']['asset']['modality'] = ''
    a['aia']['setup']['indications'][1]['include'] = False
    assert client.put('/api/assets/SYN-101', json=a, headers=EDITOR).status_code == 200
    assert rows(structx, "SELECT COUNT(*) FROM input_value v JOIN input i USING (input_id) WHERE i.id = 'therapeutic_modality'") == [(0,)]
    assert rows(structx, 'SELECT "include" FROM indication WHERE indication_id = 2') == [(0,)]


@pytest.mark.parametrize('edit', [
    lambda w: w['v6']['asset'].update(modality='Gene editing'),                                 # not an option of the input
    lambda w: w['setup']['indications'][0].update(launchUSMonth='Smarch'),                      # not a month
    lambda w: w['setup']['indications'][0].update(launchUS='20x1'),                             # not a year
    lambda w: w['setup']['indications'][0]['v6'].update(trialDesign=['x']),                     # wrong shape
    lambda w: [i.update(include=False) for i in w['setup']['indications']],                     # nothing included
    lambda w: w['setup']['indications'].append({'id': 'IND-1', 'name': 'Twice'}),               # id used twice
    lambda w: w['setup']['indications'].append({'id': 'X-9', 'name': 'Bad id'}),                # not an indication id
    lambda w: w['setup']['indications'][0].update(name=''),                                     # no name
    lambda w: w['v6']['comp'][0].update(vsId='IND-77'),                                         # unknown indication
    lambda w: w['setup'].update(stage='x' * 41),                                                # too long
])
def test_invalid_saves_are_refused_and_store_nothing(client, structx, edit):
    a = get_asset(client)
    a['aia']['setup']['stage'] = 'Phase 3'  # a valid change in the same request must not be kept either
    edit(a['aia'])
    before = rows(structx, 'SELECT * FROM input_value ORDER BY value_id')
    assert client.put('/api/assets/SYN-101', json=a, headers=EDITOR).status_code == 422
    assert rows(structx, 'SELECT * FROM input_value ORDER BY value_id') == before
    assert rows(structx, 'SELECT stage FROM asset WHERE asset_id = 1') == [('Phase 2',)]
    assert rows(structx, 'SELECT COUNT(*) FROM audit_log') == [(0,)]


def test_save_authorization(client, structx):
    a = get_asset(client)
    assert client.put('/api/assets/SYN-101', json=a).status_code == 401
    assert client.put('/api/assets/SYN-101', json=a, headers=VIEWER).status_code == 403  # view-only role
    assert client.put('/api/assets/SYN-202', json={**a, 'id': 'SYN-202'}, headers=EDITOR).status_code == 404  # no grant
    assert client.put('/api/assets/NOPE-1', json=a, headers=ADMIN).status_code == 404
    assert client.put('/api/assets/SYN-101', json={**a, 'id': 'SYN-202'}, headers=EDITOR).status_code == 422  # id does not match
    assert client.put('/api/assets/SYN-101', json=[], headers=EDITOR).status_code == 422
    assert client.put('/api/assets/SYN-101', json=a, headers=ADMIN).status_code == 200  # admin: every asset, no structx user needed
    # a user whose structx role cannot edit is refused even with a grant
    c = sqlite3.connect(structx)
    c.execute("UPDATE role SET can_edit = 0 WHERE code = 'editor'")
    c.commit()
    c.close()
    assert client.put('/api/assets/SYN-101', json=a, headers=EDITOR).status_code == 403


def test_not_configured_save_fails_closed(client, monkeypatch):
    monkeypatch.setattr(get_settings(), 'structx_db_path', '')
    assert client.put('/api/assets/SYN-101', json={'id': 'SYN-101'}, headers=EDITOR).status_code == 503
    assert client.patch('/api/documents/1', json={}, headers=EDITOR).status_code == 503


def test_stored_fields_and_questions_are_not_accepted(client, structx):
    assert client.put('/api/assets/SYN-101/fields', json=[], headers=EDITOR).json() == []
    assert client.put('/api/assets/SYN-101/questions', json=[], headers=EDITOR).json() == []
    assert client.put('/api/assets/SYN-101/fields', json=[{'id': 'x'}], headers=EDITOR).status_code == 422  # never silently dropped
    assert client.put('/api/assets/SYN-202/fields', json=[], headers=EDITOR).status_code == 404
    assert client.put('/api/assets/SYN-101/fields', json=[], headers=VIEWER).status_code == 403
    assert client.patch('/api/fields/anything', json={}, headers=EDITOR).status_code == 422
    assert client.patch('/api/questions/anything', json={}, headers=EDITOR).status_code == 422


def doc(types, asset='SYN-101'):
    return {'assetId': asset, 'name': 'fc.xlsx', 'size': '1 KB', 'type': types[0], 'types': types, 'scope': 'Program-wide',
            'version': 'Latest', 'tag': '', 'pages': '—', 'added': 'x'}


def test_relabelling_a_document(client, structx):
    r = client.patch('/api/documents/2', json=doc(['Forecast', 'CI']), headers=EDITOR)
    assert r.status_code == 200, r.text
    assert r.json()['types'] == ['Forecast', 'CI'] and r.json()['type'] == 'Forecast'
    assert rows(structx, 'SELECT type_code, position FROM document_file_type WHERE document_id = 2 ORDER BY position') == [('Forecast', 1), ('CI', 2)]
    assert [e for (e,) in rows(structx, 'SELECT event FROM audit_log')] == ['updated labels for fc.xlsx']
    assert client.patch('/api/documents/2', json=doc(['CDP']), headers=EDITOR).status_code == 409  # that slot is taken by deck.pptx
    assert client.patch('/api/documents/2', json=doc(['Nope']), headers=EDITOR).status_code == 422
    assert client.patch('/api/documents/2', json={**doc(['CI']), 'types': []}, headers=EDITOR).status_code == 200  # falls back to `type`
    assert client.patch('/api/documents/2', json=doc(['CI'], asset='SYN-202'), headers=EDITOR).status_code == 422  # cannot move
    assert client.patch('/api/documents/2', json=doc(['CI']), headers=VIEWER).status_code == 403
    assert client.patch('/api/documents/99', json=doc(['CI']), headers=ADMIN).status_code == 404


# ---------------- dropdown options come from structx

def test_lookups_come_from_the_structx_option_tables(client, structx):
    for headers in (ADMIN, EDITOR, VIEWER):  # the same options for everyone: they are not data of any asset
        got = client.get('/api/structx/lookups', headers=headers).json()
        assert 'therapeuticAreas' not in got  # the therapeutic area is no longer part of the catalogue
        assert got['modalities'] == ['Biologic', 'Small molecule']
        assert got['options']['trialDesign'] == ['Head-to-head', 'Placebo-controlled']  # by the app's catalogue key
        assert got['options']['cPhase'] == ['Phase II', 'Phase III', 'Launched']
        assert 'euPrice' not in got['options'] and 'launchEU' not in got['options']  # inputs without an option list are left out
    assert client.get('/api/structx/lookups').status_code == 401


def test_lookups_fail_closed_when_not_configured(client, monkeypatch):
    monkeypatch.setattr(get_settings(), 'structx_db_path', '')
    assert client.get('/api/structx/lookups', headers=ADMIN).status_code == 503


# ---------------- GET /api/assets/{code} and /api/assets/{code}/documents: the per-asset reads used when an asset is selected

def test_one_asset_and_its_documents_are_read_fresh(client, structx):
    one = client.get('/api/assets/SYN-101', headers=EDITOR)
    assert one.status_code == 200 and one.json() == get_asset(client)  # the same shape as the list entry
    docs = client.get('/api/assets/SYN-101/documents', headers=EDITOR).json()
    assert [(d['assetId'], d['name'], d['types']) for d in docs] == [('SYN-101', 'deck.pptx', ['CDP']), ('SYN-101', 'fc.xlsx', ['Forecast'])]
    # a change made outside the app (e.g. the AI team's extraction) shows on the next read, without signing in again
    c = sqlite3.connect(structx)
    c.execute("UPDATE asset SET stage = 'Phase 3' WHERE asset_id = 1")
    c.execute("INSERT INTO document VALUES (3, 1, 'new.pdf', 2048, '.pdf', 0, NULL, ?, NULL)", (NOW,))
    c.execute("INSERT INTO document_file_type VALUES (3, 'CI', 1)")
    c.commit()
    c.close()
    assert client.get('/api/assets/SYN-101', headers=EDITOR).json()['stage'] == 'Phase 3'
    assert [d['name'] for d in client.get('/api/assets/SYN-101/documents', headers=EDITOR).json()][-1] == 'new.pdf'


def test_per_asset_reads_are_authorized(client, structx):
    for path in ('/api/assets/SYN-101', '/api/assets/SYN-101/documents'):
        assert client.get(path).status_code == 401
        assert client.get(path, headers=VIEWER).status_code == 404  # no grant: looks like it does not exist
        assert client.get(path.replace('101', '999'), headers=ADMIN).status_code == 404
    for path in ('/api/assets/SYN-202', '/api/assets/SYN-202/documents'):
        assert client.get(path, headers=EDITOR).status_code == 404
        assert client.get(path, headers=ADMIN).status_code == 200


def test_per_asset_reads_fail_closed_when_not_configured(client, monkeypatch):
    monkeypatch.setattr(get_settings(), 'structx_db_path', '')
    assert client.get('/api/assets/SYN-101', headers=ADMIN).status_code == 503
    assert client.get('/api/assets/SYN-101/documents', headers=ADMIN).status_code == 503


# ---------------- new schema: selects tolerate letter case; year / month conversion; multi-select

def test_an_old_therapeutic_area_from_the_app_is_ignored(client, structx):
    """The AI team removed the therapeutic area from the catalogue: nothing about it is stored or sent."""
    a = get_asset(client)
    a['aia']['setup']['area'] = 'Immunology'  # an older copy of the app may still send it
    a['therapeuticArea'] = 'Immunology'
    r = client.put('/api/assets/SYN-101', json=a, headers=EDITOR)
    assert r.status_code == 200, r.text
    assert 'therapeuticArea' not in r.json() and 'area' not in r.json()['aia']['setup']
    assert rows(structx, "SELECT COUNT(*) FROM input_value v JOIN input i USING (input_id) WHERE i.id = 'therapeutic_area'") == [(0,)]


def test_selects_accept_another_letter_case_and_store_structx_spelling(client, structx):
    a = get_asset(client)
    a['aia']['v6']['asset']['modality'] = 'small molecule'
    a['aia']['setup']['indications'][0]['v6']['trialDesign'] = 'placebo-controlled'  # the old demo spelling
    assert client.put('/api/assets/SYN-101', json=a, headers=EDITOR).status_code == 200
    got = dict(rows(structx, "SELECT i.id, v.value FROM input_value v JOIN input i USING (input_id) WHERE i.id IN ('therapeutic_modality', 'clinical_trial_comparator_design')"))
    assert got == {'therapeutic_modality': '"Small molecule"', 'clinical_trial_comparator_design': '"Placebo-controlled"'}


def test_years_and_year_months_convert_both_ways(client, structx):
    a = get_asset(client)
    first = a['aia']['setup']['indications'][0]
    assert (first['launchUS'], first['launchUSMonth'], first['launchEU']) == ('2030', 'May', '2031')  # { year, month } and an integer -> text
    first['launchUS'], first['launchUSMonth'], first['launchEU'] = '2032', '', '2033'
    assert client.put('/api/assets/SYN-101', json=a, headers=EDITOR).status_code == 200
    got = dict(rows(structx, "SELECT i.id, v.value FROM input_value v JOIN input i USING (input_id) WHERE v.indication_id = 1 AND i.id IN ('us_launch_timing', 'eu_launch_timing')"))
    assert got == {'us_launch_timing': '{"year":2032,"month":null}', 'eu_launch_timing': '2033'}  # stored as structx expects
    first['launchUS'] = ''
    assert client.put('/api/assets/SYN-101', json=a, headers=EDITOR).status_code == 200  # an empty year clears the value
    assert rows(structx, "SELECT COUNT(*) FROM input_value v JOIN input i USING (input_id) WHERE v.indication_id = 1 AND i.id = 'us_launch_timing'") == [(0,)]


def test_multi_select_values_are_checked_against_the_options():
    from app.structx_write import CLEAR, coerce
    options = {'United States', 'France', 'Germany'}
    assert coerce('multi_select', ['France', 'Germany'], options) == ['France', 'Germany']
    assert coerce('multi_select', [], options) is CLEAR
    for bad in (['Spain'], ['France', 'France'], 'France', [1]):
        with pytest.raises(ValueError):
            coerce('multi_select', bad, options)


# ---------------- assessment actions are recorded in structx (not the app's own tables)

def test_assessment_actions_are_recorded_in_the_structx_audit_log(client, structx):
    assert client.post('/api/assets/SYN-101/eligibility/run', headers=EDITOR).status_code == 204
    assert client.post('/api/assets/SYN-101/impact/run', headers=EDITOR).status_code == 204
    assert client.post('/api/assets/SYN-101/extract', headers=EDITOR).json() == {'status': 'requested'}
    scenario = {'name': 'Scenario 1', 'time': '1 Jan, 10:00', 'by': 'Demo Editor', 'vals': {'usL': 2031, 'seq': 'EU-first'}, 'delta': -12.5}
    saved = client.post('/api/assets/SYN-101/scenarios', json=scenario, headers=EDITOR)
    assert saved.status_code == 201 and saved.json() == scenario
    assert [e for (e,) in rows(structx, 'SELECT event FROM audit_log ORDER BY log_id')] == [
        'ran eligibility assessment', 'ran impact analysis', 'requested extraction', 'saved scenario']
    assert {u for (u,) in rows(structx, 'SELECT DISTINCT user_id FROM audit_log')} == {1}  # the structx user matched by e-mail
    assert 'Scenario 1' not in str(rows(structx, 'SELECT * FROM audit_log'))  # the scenario's content is not written anywhere


def test_assessment_actions_check_access_and_fail_closed(client, structx, monkeypatch):
    for path in ('eligibility/run', 'impact/run', 'extract'):
        assert client.post(f'/api/assets/SYN-101/{path}').status_code == 401
        assert client.post(f'/api/assets/SYN-101/{path}', headers=VIEWER).status_code == 403  # view-only role
        assert client.post(f'/api/assets/SYN-202/{path}', headers=EDITOR).status_code == 404  # no grant on this asset
        assert client.post(f'/api/assets/NOPE-1/{path}', headers=ADMIN).status_code == 404
    assert rows(structx, 'SELECT COUNT(*) FROM audit_log') == [(0,)]
    monkeypatch.setattr(get_settings(), 'structx_db_path', '')
    assert client.post('/api/assets/SYN-101/impact/run', headers=EDITOR).status_code == 503


# ---------------- Overview stage: the most advanced phase of the included indications

def test_stage_is_derived_from_the_indications_when_the_asset_has_none(client, structx):
    assert get_asset(client)['stage'] == 'Phase 2'  # a stage structx holds is shown as it is
    c = sqlite3.connect(structx)
    c.execute('UPDATE asset SET stage = NULL WHERE asset_id = 1')
    c.execute("INSERT INTO input (input_id, id, name, category, section, level, kind, source, screen, required, expected, source_text, policies, workbook_row) "
              "VALUES (30, 'phase_of_development', 'Phase', 'Asset Description', 'asset', 'indication', 'select', 'cdp', 'submission', 0, '', '', '[]', 30)")
    c.executemany("INSERT INTO input_option (input_id, position, value) VALUES (30, ?, ?)", [(1, 'Phase II'), (2, 'Phase III')])
    c.executemany("INSERT INTO input_value (asset_id, input_id, indication_id, value, source, status, updated_at) VALUES (1, 30, ?, ?, 'cdp', 'ai_extracted', ?)",
                  [(1, '"Phase II"', NOW), (3, '"Phase III"', NOW)])  # indication 3 is unticked, so it does not count
    c.commit()
    c.close()
    assert get_asset(client)['stage'] == 'Phase II'
    c = sqlite3.connect(structx)
    c.execute('UPDATE indication SET "include" = 1 WHERE indication_id = 3')
    c.commit()
    c.close()
    assert get_asset(client)['stage'] == 'Phase III'
    assert get_asset(client)['aia']['setup']['stage'] == ''  # the saved stage itself is untouched, so a save never writes the derived one


def test_creating_an_asset(client, structx):
    r = client.post('/api/assets', json={'code': 'SYN-303'}, headers=EDITOR)
    assert r.status_code == 201, r.text
    assert r.json()['id'] == 'SYN-303'
    assert rows(structx, "SELECT name, asset_name FROM asset WHERE name = 'SYN-303'") == [('SYN-303', 'SYN-303')]
    assert rows(structx, "SELECT event FROM audit_log WHERE event = 'created asset'") == [('created asset',)]
    # the creator is the owner and creator, has access to it, and is the user of the audit row
    assert rows(structx, "SELECT owner_user_id, created_by FROM asset WHERE name = 'SYN-303'") == [(1, 1)]
    assert rows(structx, "SELECT user_id FROM user_asset WHERE asset_id = (SELECT asset_id FROM asset WHERE name = 'SYN-303')") == [(1,)]
    assert rows(structx, "SELECT user_id FROM audit_log WHERE event = 'created asset'") == [(1,)]
    # the creator can open it, and the set-up is then saved with the normal PUT (an asset with no indications yet)
    a = get_asset(client, 'SYN-303')
    assert client.put('/api/assets/SYN-303', json=a, headers=EDITOR).status_code == 200


def test_an_asset_is_not_created_for_someone_without_a_user_row(client, structx):
    # the Admin of the app's own database has no active row in structx: refused, never an asset without an owner
    r = client.post('/api/assets', json={'code': 'SYN-404'}, headers=ADMIN)
    assert r.status_code == 403 and 'User management' in r.json()['detail']
    assert rows(structx, 'SELECT COUNT(*) FROM asset') == [(2,)]


def test_creating_an_asset_is_checked(client, structx):
    assert client.post('/api/assets', json={'code': 'SYN-303'}).status_code == 401
    assert client.post('/api/assets', json={'code': 'SYN-303'}, headers=VIEWER).status_code == 403
    assert client.post('/api/assets', json={'code': 'syn-101'}, headers=EDITOR).status_code == 409  # same name, any case
    for bad in ({}, {'code': ''}, {'code': "x'; DROP TABLE asset;--"}, {'code': 'a' * 41}, {'code': 7}, []):
        assert client.post('/api/assets', json=bad, headers=EDITOR).status_code == 422, bad
    assert rows(structx, 'SELECT COUNT(*) FROM asset') == [(2,)]


# ---------------- uploading a document

def upload(client, asset='SYN-101', type_='CI', name='ci.pdf', data=b'%PDF-1.4 synthetic', headers=EDITOR):
    return client.post(f'/api/assets/{asset}/documents', data={'type': type_}, files={'file': (name, data)}, headers=headers)


@pytest.fixture()
def uploads(structx, tmp_path, monkeypatch):
    folder = tmp_path / 'uploads'
    monkeypatch.setattr(get_settings(), 'upload_dir', str(folder))
    return folder


def test_uploading_a_document(client, structx, uploads):
    r = upload(client)
    assert r.status_code == 201, r.text
    assert r.json()['name'] == 'ci.pdf' and r.json()['type'] == 'CI' and r.json()['assetId'] == 'SYN-101'
    assert (uploads / 'SYN-101' / 'ci.pdf').read_bytes() == b'%PDF-1.4 synthetic'
    assert rows(structx, "SELECT file_name, size_bytes, suffix, macro_enabled FROM document WHERE file_name = 'ci.pdf'") == [('ci.pdf', 18, '.pdf', 0)]
    assert rows(structx, "SELECT type_code, position FROM document_file_type WHERE document_id = (SELECT document_id FROM document WHERE file_name = 'ci.pdf')") == [('CI', 1)]
    assert rows(structx, "SELECT event FROM audit_log") == [('uploaded ci.pdf',)]


@pytest.mark.parametrize('kwargs, code', [
    (dict(name='run.exe', data=b'MZ'), 422),  # extension not accepted by the type
    (dict(name='ci.pdf', data=b'MZ not a pdf'), 422),  # content does not match
    (dict(name='ci.pdf', data=b''), 422),  # empty
    (dict(type_='Nope'), 422),
    (dict(name='../../evil.pdf'), 201),  # path parts are dropped, never followed
    (dict(headers=VIEWER), 403),
    (dict(asset='SYN-202'), 404),  # no grant
    (dict(type_='CDP', name='deck2.pdf'), 409),  # SYN-101 already has a CDP
])
def test_uploads_are_checked(client, structx, uploads, kwargs, code):
    assert upload(client, **kwargs).status_code == code
    assert not [p for p in uploads.rglob('*') if p.is_file() and p.parent != uploads / 'SYN-101']  # nothing outside the asset folder
    if code != 201:
        assert not [p for p in uploads.rglob('*') if p.is_file()]  # a refused upload leaves no file
        assert rows(structx, 'SELECT COUNT(*) FROM document') == [(2,)]


def test_upload_refused_when_not_configured(client, structx, monkeypatch):
    monkeypatch.setattr(get_settings(), 'upload_dir', '')
    assert upload(client).status_code == 503


def test_too_large_upload_is_refused_and_removed(client, structx, uploads, monkeypatch):
    monkeypatch.setattr(get_settings(), 'max_upload_bytes', 20)
    assert upload(client, data=b'%PDF' + b'x' * 100).status_code == 413
    assert not [p for p in uploads.rglob('*') if p.is_file()]
    assert rows(structx, 'SELECT COUNT(*) FROM document') == [(2,)]


def test_macro_file_is_flagged(client, structx, uploads):
    assert upload(client, type_='Forecast', name='f.xlsm', data=b'PK synthetic', asset='SYN-101').status_code == 409  # Forecast is taken
    c = sqlite3.connect(structx); c.execute('DELETE FROM document_file_type WHERE document_id = 2'); c.execute('DELETE FROM document WHERE document_id = 2'); c.commit(); c.close()
    assert upload(client, type_='Forecast', name='f.xlsm', data=b'PK synthetic').status_code == 201
    assert rows(structx, "SELECT macro_enabled FROM document WHERE file_name = 'f.xlsm'") == [(1,)]
