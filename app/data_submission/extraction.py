"""Running the AI extraction on an asset's uploaded documents.

    POST /assets/{code}/extraction   start (checks first, then a background thread)       -> 202
    GET  /assets/{code}/extraction   state of the last run (+ what structx.sqlite knows)

Flow: the CDP / CI / Protocol files were uploaded to <PVO_UPLOAD_DIR>/<asset>/ (app/structx_upload.py).
  1. CDP  -> structx.steps.extract_cdp        the indication names
  2. CI and Protocol -> extract_ci / extract_protocol, in two threads, mapped to those names
  3. every output is validated (validation.py) and joined (structx combine)
  4. one transaction saves everything (storage.py). A failure at any step saves nothing.

Security notes
- The route needs a signed-in user with edit rights on that asset (checked per asset, not just per session).
- File paths come from the database rows and the configured upload folder, never from the request; each is resolved
  and must stay inside that folder.
- The gateway key is read by the package from the environment (STRUCTX_BASE_URL, STRUCTX_MODEL, STRUCTX_API_KEY).
  It is never logged. Failures are logged by exception class only and shown to users as fixed messages, because the
  gateway's error text can quote the document.
- The whole document is sent to the AI gateway: only Public / Internal-Non-Sensitive files may be uploaded.
- The run state is kept in memory: it is lost on restart and is not shared between worker processes.
"""
import logging
import sqlite3
import threading
import traceback
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException, status

from .. import structx_repo as repo
from ..security import CurrentUser
from ..structx_upload import _root, asset_folder, ensure_path_column
from . import storage
from .validation import InvalidExtraction, check_names, check_output

log = logging.getLogger(__name__)

TYPES = ('CDP', 'CI', 'Protocol')
_ACCEPTED = {'.pptx', '.pdf', '.docx'}  # what the extraction package can read

_lock = threading.Lock()
_jobs: dict[str, dict] = {}


@dataclass
class Plan:
    asset: sqlite3.Row
    user_id: int | None
    docs: dict[str, dict]                       # type -> {document_id, file_name}
    paths: dict[str, Path]                      # type -> file on disk
    stored_names: list[str] = field(default_factory=list)   # the asset's indications, used when no CDP is re-run
    fields: dict[str, list[str]] = field(default_factory=dict)  # spec name -> the fields in the asset's launch scope (None = every field)
    on_cdp: Callable[[dict, list[str]], None] | None = None     # called with (CDP output, names) when the CDP is read: saves it at once
    cdp_saved: bool = False                                     # set when that save happened


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


# ---------------- checks before anything runs

def has_path_column(conn: sqlite3.Connection) -> bool:
    """True when the document table has a `file_path` column (where the saved file lives, relative to the upload folder)."""
    return any(r[1] == 'file_path' for r in conn.execute('PRAGMA table_info(document)'))


def _document(conn: sqlite3.Connection, asset_id: int, type_code: str) -> dict | None:
    path_col = ', d.file_path' if has_path_column(conn) else ", NULL AS file_path"
    row = conn.execute(f'SELECT d.document_id, d.file_name, d.suffix{path_col} FROM document d JOIN document_file_type t ON t.document_id = d.document_id '
                       'WHERE d.asset_id = ? AND t.type_code = ? ORDER BY t.position, d.document_id LIMIT 1', (asset_id, type_code)).fetchone()
    return dict(row) if row else None


def locate(root: Path, folder: Path, doc: dict) -> Path | None:
    """The file on disk for a document row: the stored `file_path` (relative to the upload folder) when there is one, else
    <asset folder>/<file name>. The result must be a file directly in the asset's own folder; anything else returns None."""
    candidates = []
    if doc.get('file_path'):
        candidates.append(root / str(doc['file_path']))
    candidates.append(folder / doc['file_name'])
    for c in candidates:
        try:
            t = c.resolve()
        except (OSError, ValueError):
            continue
        if t.parent == folder.resolve() and root in t.parents and t.is_file():  # only a file directly in this asset's folder
            return t
    return None


def _config():
    """The package's settings, read from the environment. Fails closed (503) without leaking details."""
    try:
        from structx.settings.config import ConfigError, StructConfig
    except ImportError:
        log.error('the extraction package cannot be imported (dependencies not installed?)')
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, 'AI extraction is not installed on this server.') from None
    try:
        return StructConfig.from_env()
    except ConfigError:
        log.error('the extraction package is not configured')
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, 'AI extraction is not configured on this server.') from None


def _scoped_fields(conn: sqlite3.Connection, asset_id: int) -> dict[str, list[str]]:
    """The spec fields to extract for the asset: only those whose input is in its launch scope (Launch Markets on Data
    Submission: US -> Global + US, EU -> Global + EU, otherwise all). The package decides the scope; the key lists are always kept."""
    from structx.join.inputs import scoped_fields
    try:
        return {spec: scoped_fields(conn, asset_id, spec) for spec in ('cdp', 'ci', 'protocol')}
    except (sqlite3.Error, KeyError, ValueError):
        log.error('the launch scope of the asset could not be read')
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, 'The data source does not match the extraction package. Ask the AI team for the matching structx.sqlite.') from None


def prepare(conn: sqlite3.Connection, user: CurrentUser, asset: sqlite3.Row, *, replace: bool) -> Plan:
    """Everything that can be refused before the slow run starts. Raises HTTPException."""
    root = _root()  # 503 when uploads are not configured
    problem = storage.schema_problem(conn)
    if problem:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, problem)

    ensure_path_column(conn, root)  # the document table keeps where each file is saved; older rows are filled in here
    docs: dict[str, dict] = {}
    paths: dict[str, Path] = {}
    folder = asset_folder(root, asset['name']).resolve()
    for type_code in TYPES:
        doc = _document(conn, asset['asset_id'], type_code)
        if doc is None:
            continue
        if doc['suffix'] not in _ACCEPTED:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                f"The {type_code} file must be .pptx, .pdf or .docx for AI extraction (it is {doc['suffix']}).")
        target = locate(root, folder, doc)
        if target is None:
            log.error('document %s of type %s was not found in the upload folder', doc['document_id'], type_code)
            raise HTTPException(status.HTTP_409_CONFLICT, f'The {type_code} file is missing from the upload folder.')
        docs[type_code], paths[type_code] = doc, target

    stored = [r['indication'] for r in conn.execute('SELECT indication FROM indication WHERE asset_id = ? ORDER BY indication_id', (asset['asset_id'],))]
    if not docs:
        raise HTTPException(status.HTTP_409_CONFLICT, 'Upload a CDP, CI or Protocol file first.')
    if 'CDP' not in docs and not stored:
        raise HTTPException(status.HTTP_409_CONFLICT, 'Upload the CDP file first: the indications come from it.')
    if storage.has_existing_work(conn, asset) and not replace:
        raise HTTPException(status.HTTP_409_CONFLICT, 'This asset already has extracted or entered values. Running again replaces them: confirm with replace=true.')
    su = repo.structx_user(conn, user)
    return Plan(asset=asset, user_id=su['user_id'] if su else None, docs=docs, paths=paths, stored_names=stored,
                fields=_scoped_fields(conn, asset['asset_id']))


# ---------------- the run

def _mark(plan: Plan, name: str, step_state: str) -> None:
    """Record the progress of one step (waiting / running / done / failed) for the status route. Never raises."""
    try:
        code = plan.asset['name']
    except (KeyError, IndexError, TypeError):
        return
    with _lock:
        job = _jobs.get(code)
        if not job or 'steps' not in job:
            return
        steps = []
        for s in job['steps']:
            if s['name'] == name:
                s = {**s, 'state': step_state}
                if step_state == 'running':
                    s['startedAt'] = _now()
                else:
                    s['finishedAt'] = _now()
            steps.append(s)
        _jobs[code] = {**job, 'steps': steps}


def run_steps(cfg, plan: Plan) -> storage.Outcome:
    """Call the extraction functions, validate and join the results. No database access; may take minutes."""
    from structx.join.combine import combine, indications_only
    from structx.settings.spec import load_spec
    from structx.steps import extract_cdp, extract_ci, extract_protocol

    cdp_output = None
    if 'CDP' in plan.paths:
        _mark(plan, 'CDP', 'running')
        try:
            cdp = extract_cdp(plan.paths['CDP'], cfg=cfg, write_files=False, fields=plan.fields.get('cdp'))
            cdp_output = check_output('cdp', cdp.output)
            names = check_names(cdp.indications)
        except Exception as exc:  # noqa: BLE001 - labelled with the step, then reported with a fixed message
            _mark(plan, 'CDP', 'failed')
            raise StepFailed('CDP') from exc
        if plan.on_cdp is not None:
            plan.on_cdp(cdp_output, names)  # outside the try: a saving problem is not an AI problem
        _mark(plan, 'CDP', 'done')
    else:
        names = check_names(plan.stored_names)

    def step(label: str, kind: str, fn, path: Path) -> dict:
        _mark(plan, label, 'running')
        try:
            out = check_output(kind, fn(path, names, cfg=cfg, write_files=False, fields=plan.fields.get(kind)))
        except Exception as exc:  # noqa: BLE001
            _mark(plan, label, 'failed')
            raise StepFailed(label) from exc
        _mark(plan, label, 'done')
        return out

    results: dict[str, dict] = {}
    failed: list[StepFailed] = []
    with ThreadPoolExecutor(max_workers=2) as pool:  # the two steps are independent once the names are known
        futures = {label: pool.submit(step, label, kind, fn, plan.paths[label])
                   for label, kind, fn in (('CI', 'ci', extract_ci), ('Protocol', 'protocol', extract_protocol)) if label in plan.paths}
        for label, future in futures.items():
            try:
                results[label] = future.result()
            except StepFailed as exc:
                failed.append(exc)
    if failed:  # every step has finished; report all that failed, not just the first
        raise StepFailed(' and '.join(f.step for f in failed)) from failed[0].__cause__
    ci, protocol = results.get('CI'), results.get('Protocol')

    aliases = _aliases()
    combined = None
    if ci is not None or protocol is not None:
        base = cdp_output or indications_only(names, plan.asset['cdp_file'] or '')
        try:
            combined = combine(base, ci, protocol, aliases=aliases)
        except (KeyError, TypeError, ValueError) as exc:
            raise InvalidExtraction('The CI / Protocol results could not be joined to the indications.') from exc
    return storage.Outcome(names=names, cdp_output=cdp_output, combined=combined, ran=list(plan.paths), aliases=aliases, cdp_saved=plan.cdp_saved)


def _aliases() -> dict:
    from structx.settings.spec import load_spec
    return {**load_spec('protocol').aliases, **load_spec('ci').aliases}


class StepFailed(Exception):
    """One extraction step (CDP, CI or Protocol) failed; the real error is the `__cause__`."""

    def __init__(self, step: str):
        super().__init__(step)
        self.step = step


def _local_file_problem(text: str) -> bool:
    """The package's messages about the LOCAL file (not the model): safe to log, and they say why a file could not be read."""
    return text.startswith(('Could not save', 'Could not read', 'Could not leave', 'Unsupported file type')) or text.endswith((' gave an empty PDF.', ' has no visible slides.'))


def _log_failure(exc: Exception) -> str:
    """Class names and the step only. The text of an error is logged only for local file problems, never for gateway or model errors."""
    step = f' in step {exc.step}' if isinstance(exc, StepFailed) else ''
    cause = exc.__cause__ or exc
    text = str(cause)
    detail = f' ({text[:200]})' if type(cause).__name__ == 'ExtractionError' and _local_file_problem(text) else ''
    if isinstance(cause, sqlite3.Error):  # a database error names tables and columns, never the document's text
        detail = f' ({str(cause)[:200]})'
    # Where it failed: file, line and function only. The code lines and the values in them are left out on purpose.
    where = ' <- '.join(f'{Path(f.filename).name}:{f.lineno} {f.name}' for f in reversed(traceback.extract_tb(cause.__traceback__)[-4:]))
    text = f'{type(cause).__name__}{detail} [{where}]'
    log.error('extraction failed%s: %s', step, text)
    return text


def _fail_message(exc: Exception) -> str:
    """A fixed, user-safe message. The exception text is never shown: it can quote the document."""
    if isinstance(exc, StepFailed):
        cause = exc.__cause__ or exc
        if isinstance(cause, InvalidExtraction):
            return f'The AI result for the {exc.step} was not in the expected format. Nothing was saved.'
        if type(cause).__name__ == 'ExtractionError':
            if str(cause).startswith('Could not save') and ' as PDF' in str(cause):  # Word / PowerPoint could not make the PDF on this server
                return (f'The {exc.step} file could not be converted to PDF on the server (Word or PowerPoint is not available to it). '
                        'Upload the file as a PDF instead. Nothing was saved.')
            return f'The AI service could not extract data from the {exc.step} document. Check the file and try again. Nothing was saved.'
        exc = cause
    name = type(exc).__name__
    if isinstance(exc, InvalidExtraction):
        return 'The AI result was not in the expected format. Nothing was saved.'
    if name == 'ConfigError':
        return 'AI extraction is not configured correctly. Nothing was saved.'
    if name == 'ExtractionError':
        return 'The AI service could not extract data from a document. Check the files and try again. Nothing was saved.'
    if isinstance(exc, HTTPException):
        return str(exc.detail)
    return 'The extraction failed unexpectedly. Nothing was saved.'


def _set(code: str, **values) -> None:
    with _lock:
        _jobs[code] = {**_jobs.get(code, {}), **values}


def _worker(cfg, plan: Plan) -> None:
    code = plan.asset['name']
    conn = None

    def save_cdp_now(cdp_output: dict, names: list[str]) -> None:
        """The CDP is read: save the indications now and tell the screen, which opens them for editing while the rest runs."""
        c = repo.open_rw()
        try:
            storage.save_cdp(c, plan.asset, plan.user_id, plan.docs, storage.Outcome(names=names, cdp_output=cdp_output, ran=['CDP'], aliases=_aliases()))
        finally:
            c.close()
        plan.cdp_saved = True
        _set(code, cdpSaved=True)

    if 'CDP' in plan.paths:
        plan.on_cdp = save_cdp_now
    try:
        outcome = run_steps(cfg, plan)
        _mark(plan, 'Save', 'running')
        conn = repo.open_rw()
        storage.save(conn, plan.asset, plan.user_id, plan.docs, outcome)
        _mark(plan, 'Save', 'done')
        _set(code, state='succeeded', finishedAt=_now(), message='The extracted data was saved.')
    except Exception as exc:  # noqa: BLE001 - every failure ends the run with a fixed message
        detail = _log_failure(exc)
        if not isinstance(exc, StepFailed):
            _mark(plan, 'Save', 'failed')  # a failure outside the extraction steps happened while joining or saving
        message = _fail_message(exc)
        if plan.cdp_saved:  # the indications were saved before the CI / Protocol were read
            message = message.replace('Nothing was saved.', 'The indications and CDP values were saved; the CI / Protocol values were not.')
        _set(code, state='failed', finishedAt=_now(), message=message, detail=detail)
    finally:
        if conn is not None:
            conn.close()


def start(conn: sqlite3.Connection, user: CurrentUser, asset: sqlite3.Row, *, replace: bool = False) -> dict:
    """Run every check, then start the extraction in the background. One run per asset at a time (409 otherwise)."""
    code = asset['name']
    with _lock:
        if _jobs.get(code, {}).get('state') == 'running':
            raise HTTPException(status.HTTP_409_CONFLICT, 'An extraction is already running for this asset.')
    plan = prepare(conn, user, asset, replace=replace)
    cfg = _config()
    with _lock:  # re-check: another request may have started while the checks ran
        if _jobs.get(code, {}).get('state') == 'running':
            raise HTTPException(status.HTTP_409_CONFLICT, 'An extraction is already running for this asset.')
        steps = [{'name': t, 'state': 'waiting', 'startedAt': None, 'finishedAt': None} for t in TYPES if t in plan.docs]
        steps.append({'name': 'Save', 'state': 'waiting', 'startedAt': None, 'finishedAt': None})
        _jobs[code] = {'state': 'running', 'startedAt': _now(), 'finishedAt': None, 'message': 'Extraction started.', 'documents': list(plan.docs), 'steps': steps}
        state = dict(_jobs[code])
    threading.Thread(target=_worker, args=(cfg, plan), name=f'extract-{plan.asset["asset_id"]}', daemon=True).start()
    return state


def state(code: str) -> dict:
    with _lock:
        return dict(_jobs.get(code) or {'state': 'idle', 'startedAt': None, 'finishedAt': None, 'message': '', 'documents': [], 'steps': []})
