"""AI extraction of uploaded CDP / CI / Protocol files (app/data_submission). Synthetic data only; the AI calls are stubbed,
so no network, key or document is used. The one database test needs the package's own dependencies and is skipped without them."""
import sqlite3
import threading
import time

import pytest
from test_structx import ADMIN, EDITOR, NOW, VIEWER, get_asset, rows, structx, upload, uploads  # noqa: F401 (fixtures)

from app.data_submission import extraction, storage
from app.data_submission.validation import InvalidExtraction, check_names, check_output

URL = '/api/assets/SYN-101/extraction'


# ---------------- validation (pure)

def cdp(*names, **extra):
    return {'source': {'spec': 'cdp'}, 'summary': {}, 'indications': [{'indication': {'value': n}} for n in names], **extra}


def test_valid_outputs_pass():
    assert check_output('cdp', cdp('A', 'B'))['indications'][1]['indication']['value'] == 'B'
    assert check_output('ci', {'source': {'spec': 'ci'}, 'summary': {}, 'competitors': []})
    assert check_output('protocol', {'source': {'spec': 'protocol'}, 'summary': {}, 'studies': [{'study': {'value': ['S1', 'S2']}}]})
    assert check_names([' A ', 'A', 'B']) == ['A', 'B']


@pytest.mark.parametrize('kind, out', [
    ('cdp', None), ('cdp', []), ('cdp', {}),
    ('cdp', cdp()),                                                  # no indication at all
    ('cdp', cdp('A', source={'spec': 'ci'})),                        # the wrong kind of result
    ('cdp', {'source': {'spec': 'cdp'}, 'indications': [{'indication': {'value': 'A'}}]}),  # no summary
    ('cdp', {'source': {'spec': 'cdp'}, 'summary': {}, 'indications': [{'indication': {'value': '  '}}]}),
    ('cdp', {'source': {'spec': 'cdp'}, 'summary': {}, 'indications': ['text']}),
    ('cdp', cdp('x' * 501)),
    ('ci', {'source': {'spec': 'ci'}, 'summary': {}, 'competitors': [{'competitor': {'value': None}}]}),
    ('protocol', {'source': {'spec': 'protocol'}, 'summary': {}, 'studies': 'many'}),
    ('protocol', {'source': {'spec': 'protocol'}, 'summary': {}, 'studies': [{}] * 2001}),
])
def test_malformed_outputs_are_rejected(kind, out):
    with pytest.raises(InvalidExtraction):
        check_output(kind, out)


@pytest.mark.parametrize('names', [None, [], 'A', [''], [1], ['x' * 501], [f'n{i}' for i in range(101)]])
def test_bad_indication_names_are_rejected(names):
    with pytest.raises(InvalidExtraction):
        check_names(names)


# ---------------- the route

@pytest.fixture()
def stubbed(monkeypatch):
    """The AI steps and the final save replaced; the checks, the thread and the status are the real ones."""
    calls: dict = {}
    extraction._jobs.clear()
    monkeypatch.setattr(storage, 'schema_problem', lambda conn: None)
    monkeypatch.setattr(extraction, '_config', lambda: object())
    monkeypatch.setattr(extraction, '_scoped_fields', lambda conn, asset_id: {'cdp': ['us_launch_timing'], 'ci': ['phase_of_development'], 'protocol': []})

    def run_steps(cfg, plan):
        calls['plan'] = plan
        return storage.Outcome(names=['Indication A'], ran=list(plan.paths))

    monkeypatch.setattr(extraction, 'run_steps', run_steps)
    monkeypatch.setattr(storage, 'save', lambda conn, asset, uid, docs, out: calls.update(saved=(asset['name'], uid, sorted(docs), out.names)))
    return calls


def put_deck(uploads):  # noqa: F811
    (uploads / 'SYN-101').mkdir(parents=True)
    (uploads / 'SYN-101' / 'deck.pptx').write_bytes(b'PK\x03\x04 synthetic')


def wait(client, headers=EDITOR):
    for _ in range(200):
        job = client.get(URL, headers=headers).json()['job']
        if job['state'] != 'running':
            return job
        time.sleep(0.02)
    raise AssertionError('the extraction is still running')


def test_requires_sign_in_and_edit_rights(client, structx, uploads, stubbed):  # noqa: F811
    put_deck(uploads)
    assert client.post(URL).status_code == 401
    assert client.get(URL).status_code == 401
    assert client.post(URL + '?replace=true', headers=VIEWER).status_code in (403, 404)  # no grant on this asset
    assert client.post('/api/assets/SYN-202/extraction', headers=EDITOR).status_code == 404  # editor has no grant on SYN-202
    assert 'plan' not in stubbed


def test_runs_validates_and_saves(client, structx, uploads, stubbed):  # noqa: F811
    put_deck(uploads)
    r = client.post(URL + '?replace=true', headers=EDITOR)
    assert r.status_code == 202, r.text
    job = wait(client)
    assert job['state'] == 'succeeded', job
    plan = stubbed['plan']
    assert list(plan.paths) == ['CDP'] and plan.paths['CDP'].name == 'deck.pptx'
    assert plan.fields['cdp'] == ['us_launch_timing']  # the launch-scope fields are carried into the plan
    assert stubbed['saved'] == ('SYN-101', 1, ['CDP'], ['Indication A'])  # the editor's structx user id is passed on


def test_a_second_run_is_refused_while_one_is_running(client, structx, uploads, stubbed, monkeypatch):  # noqa: F811
    put_deck(uploads)
    gate = threading.Event()
    monkeypatch.setattr(extraction, 'run_steps', lambda cfg, plan: (gate.wait(5), storage.Outcome(names=['A'], ran=['CDP']))[1])
    assert client.post(URL + '?replace=true', headers=EDITOR).status_code == 202
    assert client.post(URL + '?replace=true', headers=EDITOR).status_code == 409
    gate.set()
    assert wait(client)['state'] == 'succeeded'


def test_failures_save_nothing_and_never_show_the_error_text(client, structx, uploads, stubbed, monkeypatch):  # noqa: F811
    put_deck(uploads)

    def boom(cfg, plan):
        raise RuntimeError('quote from the document: SYNTHETIC-SECRET')

    monkeypatch.setattr(extraction, 'run_steps', boom)
    assert client.post(URL + '?replace=true', headers=EDITOR).status_code == 202
    job = wait(client)
    assert job['state'] == 'failed' and 'SYNTHETIC-SECRET' not in str(job) and 'Nothing was saved' in job['message']
    assert 'saved' not in stubbed


def test_invalid_ai_output_is_a_fixed_message(client, structx, uploads, stubbed, monkeypatch):  # noqa: F811
    put_deck(uploads)

    def bad(cfg, plan):
        raise InvalidExtraction('detail that must not be shown')

    monkeypatch.setattr(extraction, 'run_steps', bad)
    client.post(URL + '?replace=true', headers=EDITOR)
    job = wait(client)
    assert job['state'] == 'failed' and 'detail' not in job['message'] and 'expected format' in job['message']


def _fake_package(monkeypatch, *, cdp_error=None, ci_error=None, protocol_error=None, calls=None):
    """Stand-ins for structx.steps / structx.join.combine / structx.settings.spec, so run_steps runs without the AI package's libraries."""
    import sys
    import types

    class ExtractionError(Exception):  # same class name as the package's, which is what the messages are chosen by
        pass

    def make(kind, error):
        def fn(*args, **kwargs):
            if calls is not None:
                calls[kind] = kwargs.get('fields')
            if error:
                raise ExtractionError(error)
            return {'source': {'spec': kind}, 'summary': {}, {'ci': 'competitors', 'protocol': 'studies'}[kind]: []}
        return fn

    def extract_cdp(path, **kwargs):
        if calls is not None:
            calls['cdp'] = kwargs.get('fields')
        if cdp_error:
            raise ExtractionError(cdp_error)
        return types.SimpleNamespace(output=cdp('A'), indications=['A'])

    steps = types.ModuleType('structx.steps')
    steps.extract_cdp, steps.extract_ci, steps.extract_protocol = extract_cdp, make('ci', ci_error), make('protocol', protocol_error)
    comb = types.ModuleType('structx.join.combine')
    comb.combine = lambda base, ci, protocol, aliases=None: {'joined': True}
    comb.indications_only = lambda names, file: cdp(*names)
    spec = types.ModuleType('structx.settings.spec')
    spec.load_spec = lambda name: types.SimpleNamespace(aliases={})
    for name, mod in (('structx.steps', steps), ('structx.join.combine', comb), ('structx.settings.spec', spec)):
        monkeypatch.setitem(sys.modules, name, mod)


def _plan(tmp_path):
    p = tmp_path / 'f.bin'
    return extraction.Plan(asset={'cdp_file': ''}, user_id=None, docs={}, paths={'CDP': p, 'CI': p, 'Protocol': p}, stored_names=[])


def test_a_failed_step_is_named(monkeypatch, tmp_path):
    _fake_package(monkeypatch, protocol_error='SYNTHETIC-SECRET model text')
    with pytest.raises(extraction.StepFailed) as e:
        extraction.run_steps(object(), _plan(tmp_path))
    assert e.value.step == 'Protocol'
    msg = extraction._fail_message(e.value)
    assert 'Protocol document' in msg and 'SYNTHETIC-SECRET' not in msg and 'Nothing was saved' in msg


def test_a_failed_pdf_conversion_tells_the_user_to_upload_a_pdf():
    class ExtractionError(Exception):
        pass

    e = extraction.StepFailed('Protocol')
    e.__cause__ = ExtractionError('Could not save protocol.docx as PDF: Word PDF export failed (is Microsoft Word installed?)')
    msg = extraction._fail_message(e)
    assert 'Upload the file as a PDF' in msg and 'Protocol file' in msg and 'Word PDF export' not in msg


def test_the_launch_scope_fields_reach_the_package_calls(monkeypatch, tmp_path):
    calls: dict = {}
    _fake_package(monkeypatch, calls=calls)
    plan = _plan(tmp_path)
    plan.fields = {'cdp': ['us_launch_timing'], 'ci': ['phase_of_development'], 'protocol': []}
    extraction.run_steps(object(), plan)
    assert calls == {'cdp': ['us_launch_timing'], 'ci': ['phase_of_development'], 'protocol': []}


def test_without_a_scope_every_field_is_extracted(monkeypatch, tmp_path):
    calls: dict = {}
    _fake_package(monkeypatch, calls=calls)
    extraction.run_steps(object(), _plan(tmp_path))  # no scope in the plan: fields=None means every field
    assert calls == {'cdp': None, 'ci': None, 'protocol': None}


def test_a_database_without_the_scope_column_is_refused():
    c = sqlite3.connect(':memory:')
    for t in ('asset', 'indication', 'cdp', 'ci', 'protocol', 'document', 'document_file_type', 'audit_log'):
        c.execute(f'CREATE TABLE {t} (x)')
    c.execute('CREATE TABLE input_value (user_id)')
    c.execute('CREATE TABLE input (id)')
    assert 'geography' in storage.schema_problem(c)
    c.execute('ALTER TABLE input ADD COLUMN geography TEXT')
    assert storage.schema_problem(c) is None


def test_the_launch_market_chooses_the_fields_with_the_package_database(tmp_path):
    pytest.importorskip('openai')
    import json

    from structx.join import database as D
    from structx.settings.spec import load_spec

    conn = sqlite3.connect(tmp_path / 'scope.sqlite', isolation_level=None)
    conn.row_factory = sqlite3.Row
    D.ensure_schema(conn, {n: load_spec(n) for n in ('cdp', 'ci', 'protocol')})
    conn.execute("INSERT INTO asset (asset_id, name, asset_name, created_at) VALUES (1, 'SYN-101', 'Synthetic', ?)", (NOW,))
    market = conn.execute("SELECT input_id FROM input WHERE id = 'launch_markets'").fetchone()[0]

    def fields(value):
        conn.execute('DELETE FROM input_value WHERE asset_id = 1')
        if value:
            conn.execute("INSERT INTO input_value (asset_id, input_id, value, source, status, updated_at) VALUES (1, ?, ?, 'user', 'entered', ?)",
                         (market, json.dumps(value), NOW))
        return extraction._scoped_fields(conn, 1)

    us, eu, both, none = fields('US'), fields('EU'), fields('Global launch (including US and EU)'), fields(None)
    assert 'us_launch_timing' in us['cdp'] and 'eu_launch_timing' not in us['cdp']
    assert 'eu_launch_timing' in eu['cdp'] and 'us_launch_timing' not in eu['cdp']
    assert set(us['cdp']) < set(both['cdp']) and set(eu['cdp']) < set(both['cdp'])
    assert set(none['cdp']) == set(both['cdp'])  # no market chosen yet: every field
    conn.close()


def test_every_failed_step_is_reported(monkeypatch, tmp_path):
    _fake_package(monkeypatch, ci_error='x', protocol_error='y')
    with pytest.raises(extraction.StepFailed) as e:
        extraction.run_steps(object(), _plan(tmp_path))
    assert e.value.step == 'CI and Protocol'


def test_a_cdp_failure_stops_before_the_other_steps(monkeypatch, tmp_path):
    _fake_package(monkeypatch, cdp_error='x')
    with pytest.raises(extraction.StepFailed) as e:
        extraction.run_steps(object(), _plan(tmp_path))
    assert e.value.step == 'CDP'


def test_all_steps_succeed(monkeypatch, tmp_path):
    _fake_package(monkeypatch)
    out = extraction.run_steps(object(), _plan(tmp_path))
    assert out.names == ['A'] and out.combined == {'joined': True} and out.ran == ['CDP', 'CI', 'Protocol']


def test_logs_name_the_step_and_only_local_file_problems(monkeypatch, tmp_path, caplog):
    class ExtractionError(Exception):
        pass

    def failure(text):
        e = extraction.StepFailed('Protocol')
        e.__cause__ = ExtractionError(text)
        return e

    with caplog.at_level('ERROR'):
        extraction._log_failure(failure('Could not save protocol.docx as PDF: Word is not available'))
        extraction._log_failure(failure('Model request failed: the model quoted SYNTHETIC-SECRET'))
    assert 'in step Protocol: ExtractionError (Could not save protocol.docx as PDF: Word is not available)' in caplog.text
    assert 'SYNTHETIC-SECRET' not in caplog.text and 'Model request failed' not in caplog.text


def test_checks_before_the_run(client, structx, uploads, stubbed):  # noqa: F811
    assert client.post(URL + '?replace=true', headers=EDITOR).status_code == 409  # the file is not in the upload folder
    put_deck(uploads)
    r = client.post(URL, headers=EDITOR)
    assert r.status_code == 409 and 'replace=true' in r.json()['detail']  # the asset already has extracted values
    assert 'plan' not in stubbed


def test_unsupported_file_type_is_refused(client, structx, uploads, stubbed):  # noqa: F811
    c = sqlite3.connect(structx)
    c.execute("UPDATE document SET file_name = 'deck.ppt', suffix = '.ppt' WHERE document_id = 1")
    c.commit()
    c.close()
    r = client.post(URL + '?replace=true', headers=EDITOR)
    assert r.status_code == 422 and '.pptx' in r.json()['detail']


def test_a_path_in_the_file_name_is_never_followed(client, structx, uploads, stubbed):  # noqa: F811
    put_deck(uploads)
    (uploads / 'secret.pptx').write_bytes(b'PK\x03\x04')
    c = sqlite3.connect(structx)
    c.execute("UPDATE document SET file_name = '../secret.pptx' WHERE document_id = 1")
    c.commit()
    c.close()
    assert client.post(URL + '?replace=true', headers=EDITOR).status_code in (400, 409)
    assert 'plan' not in stubbed


def test_not_configured_fails_closed(client, structx, uploads, monkeypatch):  # noqa: F811
    extraction._jobs.clear()
    put_deck(uploads)
    # the test database does not have the package's tables or columns: refused, with a message that names the problem
    r = client.post(URL + '?replace=true', headers=EDITOR)
    assert r.status_code == 503 and 'lacks the tables' in r.json()['detail']


def test_status_before_any_run(client, structx):  # noqa: F811
    extraction._jobs.clear()
    body = client.get(URL, headers=EDITOR).json()
    assert body['job']['state'] == 'idle' and body['extraction']['documents'][0]['type'] == 'CDP'


# ---------------- the database column (the package writes user_id, the older file has updated_by)

def test_saving_values_works_with_either_editor_column(client, structx):  # noqa: F811
    c = sqlite3.connect(structx)
    c.execute('ALTER TABLE input_value RENAME COLUMN updated_by TO user_id')
    c.commit()
    c.close()
    a = get_asset(client)
    first = a['aia']['setup']['indications'][0]
    first['launchUS'], first['launchUSMonth'] = '2031', 'Nov'
    assert client.put('/api/assets/SYN-101', json=a, headers=EDITOR).status_code == 200
    assert rows(structx, 'SELECT user_id FROM input_value WHERE value_id = 2') == [(1,)]


# ---------------- storage with the package's own tables (needs openai, python-pptx, pypdfium2 installed)

def test_storage_saves_the_extraction_in_one_transaction(tmp_path):
    pytest.importorskip('openai')
    from structx.join import database as D
    from structx.settings.spec import load_spec

    path = tmp_path / 'package.sqlite'
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    specs = {n: load_spec(n) for n in ('cdp', 'ci', 'protocol')}
    D.ensure_schema(conn, specs)
    conn.execute("INSERT INTO asset (asset_id, name, asset_name, created_at) VALUES (1, 'SYN-101', 'Synthetic', ?)", (NOW,))
    conn.execute("INSERT INTO document (document_id, asset_id, file_name, size_bytes, suffix, macro_enabled, uploaded_at) VALUES (1, 1, 'deck.pptx', 5, '.pptx', 0, ?)", (NOW,))
    conn.execute("INSERT INTO document_file_type (document_id, type_code, position) VALUES (1, 'CDP', 1)")
    assert storage.schema_problem(conn) is None

    out = storage.Outcome(names=['Indication A'], ran=['CDP'], cdp_output={
        'source': {'spec': 'cdp', 'file': 'deck.pptx', 'model': 'synthetic', 'extracted_at': NOW, 'seconds': 1},
        'summary': {}, 'other_records': [],
        'indications': [{'indication': {'value': 'Indication A', 'confidence': 'high'}}]})
    asset = conn.execute('SELECT * FROM asset WHERE asset_id = 1').fetchone()
    storage.save(conn, asset, None, {'CDP': {'document_id': 1, 'file_name': 'deck.pptx'}}, out)

    assert [r[0] for r in conn.execute('SELECT indication FROM indication WHERE asset_id = 1')] == ['Indication A']
    assert conn.execute('SELECT cdp_file FROM asset WHERE asset_id = 1').fetchone()[0] == 'deck.pptx'
    assert conn.execute('SELECT extracted_at FROM document WHERE document_id = 1').fetchone()[0]
    assert [r[0] for r in conn.execute('SELECT event FROM audit_log')] == ['ran AI extraction (CDP)']

    # A failure after the first writes (here: the document list lacks the CDP, so the save fails once load_cdp has already
    # replaced the indication rows) must roll everything back.
    out.cdp_output['indications'][0]['indication'] = {'value': 'Replaced'}
    with pytest.raises(KeyError):
        storage.save(conn, asset, None, {}, out)
    assert [r[0] for r in conn.execute('SELECT indication FROM indication WHERE asset_id = 1')] == ['Indication A']
    conn.close()


def test_the_cdp_is_saved_first_and_later_results_skip_indications_the_user_removed(tmp_path):
    pytest.importorskip('openai')
    from structx.join import database as D
    from structx.settings.spec import load_spec

    conn = sqlite3.connect(tmp_path / 'package.sqlite', isolation_level=None)
    conn.row_factory = sqlite3.Row
    D.ensure_schema(conn, {n: load_spec(n) for n in ('cdp', 'ci', 'protocol')})
    conn.execute("INSERT INTO asset (asset_id, name, asset_name, created_at) VALUES (1, 'SYN-101', 'Synthetic', ?)", (NOW,))
    conn.execute("INSERT INTO document (document_id, asset_id, file_name, size_bytes, suffix, macro_enabled, uploaded_at) VALUES (1, 1, 'deck.pptx', 5, '.pptx', 0, ?)", (NOW,))
    conn.execute("INSERT INTO document_file_type (document_id, type_code, position) VALUES (1, 'CDP', 1)")
    asset = conn.execute('SELECT * FROM asset WHERE asset_id = 1').fetchone()
    out = storage.Outcome(names=['A', 'B'], ran=['CDP'], cdp_output={
        'source': {'spec': 'cdp', 'file': 'deck.pptx', 'model': 'synthetic', 'extracted_at': NOW, 'seconds': 1},
        'summary': {}, 'other_records': [],
        'indications': [{'indication': {'value': 'A', 'confidence': 'high'}}, {'indication': {'value': 'B', 'confidence': 'high'}}]})
    storage.save_cdp(conn, asset, None, {'CDP': {'document_id': 1, 'file_name': 'deck.pptx'}}, out)
    assert out.cdp_saved
    assert [r[0] for r in conn.execute('SELECT indication FROM indication ORDER BY indication_id')] == ['A', 'B']  # editable now
    assert conn.execute('SELECT extracted_at FROM document WHERE document_id = 1').fetchone()[0]

    conn.execute("DELETE FROM indication WHERE indication = 'B'")  # the user removes B while the CI / Protocol are still being read
    kept = storage._known_indications(conn, 1, {'indications': [{'indication': {'value': 'A'}}, {'indication': {'value': 'B'}}]})
    assert [e['indication']['value'] for e in kept['indications']] == ['A']
    conn.close()


# ---------------- removing a saved document (to swap a Word protocol for its PDF)

def test_removing_a_document_deletes_its_record_and_file(client, structx, uploads):  # noqa: F811
    assert upload(client).status_code == 201  # ci.pdf
    doc_id = rows(structx, "SELECT document_id FROM document WHERE file_name = 'ci.pdf'")[0][0]
    assert (uploads / 'SYN-101' / 'ci.pdf').is_file()
    assert client.delete(f'/api/documents/{doc_id}', headers=EDITOR).status_code == 204
    assert not (uploads / 'SYN-101' / 'ci.pdf').exists()
    assert rows(structx, 'SELECT COUNT(*) FROM document WHERE document_id = ?', doc_id) == [(0,)]
    assert rows(structx, 'SELECT COUNT(*) FROM document_file_type WHERE document_id = ?', doc_id) == [(0,)]
    assert ('removed ci.pdf',) in rows(structx, 'SELECT event FROM audit_log')
    assert upload(client, name='ci2.pdf').status_code == 201  # the CI slot is free again


def test_removing_a_document_keeps_the_values_extracted_from_it(client, structx, uploads):  # noqa: F811
    before = rows(structx, 'SELECT value_id, value FROM input_value WHERE document_id = 1 ORDER BY value_id')
    assert before  # the synthetic CDP (document 1) has extracted values
    assert client.delete('/api/documents/1', headers=EDITOR).status_code == 204  # its file is not on disk: not an error
    assert rows(structx, 'SELECT value_id, value FROM input_value WHERE value_id IN (%s) ORDER BY value_id' % ','.join(str(b[0]) for b in before)) == before
    assert rows(structx, 'SELECT COUNT(*) FROM input_value WHERE document_id = 1') == [(0,)]


def test_removing_a_document_needs_sign_in_and_rights(client, structx, uploads):  # noqa: F811
    assert client.delete('/api/documents/1').status_code == 401
    assert client.delete('/api/documents/1', headers=VIEWER).status_code in (403, 404)  # no grant on this asset
    assert client.delete('/api/documents/999', headers=EDITOR).status_code == 404
    assert rows(structx, 'SELECT COUNT(*) FROM document WHERE document_id = 1') == [(1,)]


def test_a_stored_name_pointing_outside_the_upload_folder_is_never_deleted(client, structx, uploads):  # noqa: F811
    uploads.mkdir(parents=True)
    outside = uploads / 'keep.pptx'
    outside.write_bytes(b'PK')
    c = sqlite3.connect(structx)
    c.execute("UPDATE document SET file_name = '../keep.pptx' WHERE document_id = 1")
    c.commit()
    c.close()
    assert client.delete('/api/documents/1', headers=EDITOR).status_code == 204
    assert outside.is_file()  # the record is gone, the file outside the folder is untouched


# ---------------- the launch scope in the coverage numbers (the AI team's geography column)

def test_coverage_only_counts_inputs_in_the_launch_scope(client, structx):  # noqa: F811
    c = sqlite3.connect(structx)
    c.execute('ALTER TABLE input ADD COLUMN geography TEXT')
    c.execute("UPDATE input SET geography = 'Global'")
    c.execute("UPDATE input SET geography = 'US', required = 1 WHERE id = 'us_launch_timing'")
    c.execute("UPDATE input SET geography = 'EU', required = 1 WHERE id = 'eu_launch_timing'")
    c.execute("INSERT INTO input (input_id, id, name, category, section, level, kind, source, screen, geography, required, expected, source_text, policies, workbook_row) "
              "VALUES (20, 'launch_markets', 'Launch Markets', 'Regulatory', 'asset', 'asset', 'select', 'manual', 'submission', 'Global', 1, '', '', '[]', 20)")
    c.commit()

    def indications_coverage(market):
        c.execute('DELETE FROM input_value WHERE input_id = 20')
        if market:
            c.execute("INSERT INTO input_value (asset_id, input_id, value, source, status, updated_at) VALUES (1, 20, ?, 'user', 'entered', ?)", ('"%s"' % market, NOW))
        c.commit()
        return client.get('/api/structx/assets/1/coverage', headers=EDITOR).json()['screens']['indications']

    us, eu, both, none = (indications_coverage(m) for m in ('US', 'EU', 'Global launch (including US and EU)', None))
    assert us['missing'] == ['us_launch_timing']       # the EU input is not counted for a US-only launch
    assert eu['missing'] == ['eu_launch_timing']       # and the US input is not counted for an EU-only launch
    assert sorted(both['missing']) == ['eu_launch_timing', 'us_launch_timing'] and sorted(none['missing']) == sorted(both['missing'])
    c.close()


def test_an_older_database_without_geography_is_not_filtered(client, structx):  # noqa: F811
    body = client.get('/api/structx/assets/1/coverage', headers=EDITOR).json()
    assert body['screens']['indications']['missing'] == ['us_launch_timing']  # as before the scope existed


def test_file_paths_are_kept_in_the_document_table(client, structx, uploads, stubbed):  # noqa: F811
    """The run adds document.file_path when the database has none, fills it for files that exist, and uses it to find the file."""
    put_deck(uploads)
    assert client.post(URL + '?replace=true', headers=EDITOR).status_code == 202
    c = sqlite3.connect(structx)
    cols = [r[1] for r in c.execute('PRAGMA table_info(document)')]
    paths = [r[0] for r in c.execute('SELECT file_path FROM document')]
    c.close()
    assert 'file_path' in cols and paths[0] and not paths[0].startswith(('/', '..')) and ':' not in paths[0]
