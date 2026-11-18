# Using `structx` from a REST API

`structx` is a plain Python package: no server, no framework, no global state beyond the settings it reads from the environment. A REST API wraps it by calling three functions per asset, joining their results, and keeping the join in a database. This page is the integration guide; the project's `README.md` is the design document, and the `api/` package in this repository is a complete FastAPI reference implementation of everything below.

## 1. Install

```text
python -m pip install -r structx/requirements.txt
```

`structx/requirements.txt` pins the package's own dependencies only (the OpenAI client, HTTP, python-pptx, pypdfium2, python-dotenv). Add your web framework on top. Python 3.10 or newer.

Nothing is installed as a distribution: put the project folder (the one that contains `structx/`) on `sys.path` or on `PYTHONPATH`, and import with absolute names (`from structx.steps import extract_cdp`). The package finds its specs inside itself (`structx/specs/`) and its `.env` in the project folder (`structx.PROJECT_ROOT`).

Machine prerequisites, on the host that runs your API:

- **Microsoft PowerPoint and Word installed.** A `.pptx` is exported to PDF by the local PowerPoint so the model sees slide images; a `.docx` is saved as PDF by the local Word. There is no fallback for Word; without it a `.docx` run fails closed. A `.pdf` input needs neither.
- **An https endpoint that speaks the OpenAI Responses API** with streaming, structured outputs (JSON schema) and file input, and a model deployed on it that accepts PDF input. Any gateway that proxies that shape works; a Chat Completions-only endpoint does not.
- **Outbound HTTPS** to that endpoint. The whole document is sent to it, so only Public or Internal-Non-Sensitive files may go through.

## 2. Configure

Settings come from environment variables, read once by `StructConfig.from_env()`; `.env` in the project folder is loaded automatically if present (`env.sample.txt` lists every name). The three that must be set:

| Variable | Meaning |
|---|---|
| `STRUCTX_BASE_URL` | The Responses API endpoint, `https://` only. |
| `STRUCTX_MODEL` | The model or deployment name. |
| `STRUCTX_API_KEY` | The credential. Inject it from your secrets manager; never put it in code, config files or logs. |

`from_env()` raises `ConfigError` when any is missing or the URL is not https, so build the config at start-up and let a bad deployment fail before the first request. The optional variables tune timeouts, retries, concurrency and reasoning effort; the defaults are the ones measured in this project.

```python
from structx.settings.config import ConfigError, StructConfig

try:
    GATEWAY = StructConfig.from_env()
except ConfigError as exc:
    raise SystemExit(f"structx is not configured: {exc}")
```

`StructConfig` is immutable and its `repr` redacts the key. Pass the same object to every call.

## 3. The three calls

```python
from pathlib import Path
from structx.steps import extract_cdp, extract_ci, extract_protocol
from structx.model.llm import ExtractionError
from structx.settings.config import ConfigError

cdp = extract_cdp(Path(cdp_file), cfg=GATEWAY, write_files=False)             # step 1
ci = extract_ci(Path(ci_file), cdp.indications, cfg=GATEWAY, write_files=False)              # step 2a
protocol = extract_protocol(Path(pcs_file), cdp.indications, cfg=GATEWAY, write_files=False) # step 2b
```

- `extract_cdp(file)` runs the `cdp` spec on the clinical development plan and returns `CdpExtraction(output, indications)`: the output document and the distinct indication names in the deck's order. Those names are the key every other file is mapped to.
- `extract_ci(file, indications)` runs the `ci` spec on the competitive-intelligence deck with the names injected as the allowed values of every `indication` sub-field, so the model assigns each competitor record to one of them.
- `extract_protocol(file, indications)` does the same for the trial protocol concept sheet, per study.

Accepted inputs are `.pptx`, `.pdf` and `.docx`. Step 2a and 2b are independent and can run at the same time; both need step 1's names first. Each function is **synchronous and slow**: a run takes tens of seconds to a few minutes and spends most of it waiting on the gateway (the spec's fields are already extracted in parallel inside the call, up to `STRUCTX_EXTRACT_CONCURRENCY` requests at once). In an async framework run them in a worker thread (FastAPI does this for a plain `def` endpoint; otherwise `asyncio.to_thread`), and run step 2a and 2b in two threads. Do not make several extraction calls for the same asset at once; serialise them per asset.

`write_files=False` writes nothing to disk: no JSON, HTML or run log. With the default `write_files=True` each call writes `<stem>_<ext>_<spec>_extraction.json`, `.html` and `_run_log.md` next to the file (or in `out_dir`); the run log contains document content, so if you keep it, treat it like the document.

Errors are the module's own and should map to HTTP statuses:

| Exception | Raised when | Suggested status |
|---|---|---|
| `ConfigError` (`structx.settings.config`) | settings or a spec are unusable, or `indications` is empty; raised before any gateway call | 500 (deployment problem) or 409 when the indication list is the caller's |
| `ExtractionError` (`structx.model.llm`) | the file does not exist or has another suffix (raised before any call), the run failed after retries, or the CDP found no indication | 400 for the file checks, else 502 |

All three fail closed: they return a result or raise, never `None`.

## 4. The join

```python
from structx.join.combine import combine
from structx.settings.spec import load_spec

aliases = {**load_spec("protocol").aliases, **load_spec("ci").aliases}
combined = combine(cdp.output, ci, protocol, aliases=aliases)
```

`combine` is pure code with no gateway call. It attaches each competitor and each study to the CDP indication the model named, checks that name against the CDP's wording and can only lower the mapping confidence, and returns one document: the CDP entries, each with its `competitors` and `studies`. Pass `ci`, `protocol` or both. Records the model could not place stay in the result under `competitors_unmapped` and `studies_unmapped`, so nothing is silently dropped. `combine.indications_only(names, file)` builds the minimal CDP input when you only have the names (for example re-running CI alone against an asset whose indications are already stored).

## 5. Storing the result

The package writes its own SQLite tables and reads an asset back; the schema is generated from the specs, so a spec change changes the tables (`docs/schema.sql` is the tracked copy).

```python
import sqlite3
from structx.join import database as D
from structx.join import inputs as I
from structx.settings.spec import load_spec

SPECS = {name: load_spec(name) for name in ("cdp", "ci", "protocol")}

conn = sqlite3.connect(db_path, check_same_thread=False)
D.ensure_schema(conn, SPECS)          # CREATE TABLE IF NOT EXISTS + seeds the input catalogue; call once at start-up
conn.commit()

D.load_cdp(conn, cdp.output, SPECS["cdp"], asset="TAK-226", files={"cdp": cdp_file_name}, aliases=aliases)
I.copy_extracted(conn, D.asset_row(conn, "TAK-226")["asset_id"], "cdp", document_id=None)
D.load_joined(conn, combined, "ci", SPECS["ci"], asset="TAK-226")
I.copy_extracted(conn, D.asset_row(conn, "TAK-226")["asset_id"], "ci")
conn.commit()

view = D.read_asset(conn, "TAK-226", SPECS)   # the asset, its indications and every table's rows, as dicts
```

- `load_cdp` replaces one asset's `indication` and `cdp` rows (and clears its `ci` / `protocol` rows, which were mapped to the old indications); the asset row itself survives. `load_joined` replaces one of `ci` / `protocol` for the asset. Neither touches another asset, and neither commits: wrap the calls of one request in a transaction and roll back on failure.
- `inputs.copy_extracted` copies the extracted cells into the `input_value` table, the one the UI edits, keyed by the business workbook's input catalogue. Skip it if you only want the raw extraction tables.
- `write_sqlite(combined, SPECS, path, asset=...)` does the whole of the above into a fresh file, for a one-shot export.
- SQLite is single-writer: keep one connection per process behind a lock (the reference `api/store.py` does exactly this), or point at a server database later; the SQL is plain enough that a port is a connection swap plus the `?` placeholder style.

The database holds document content (quotes, values). Store it where the documents themselves may be stored.

## 6. Each `steps.py` function as its own endpoint

One stateless endpoint per function: the API only turns an upload into a call and the result into JSON. The
client keeps the pieces between calls, exactly as the functions do in code: the `indications` of `/cdp` go into
`/ci` and `/protocol`, and the three outputs go into `/combine`. No database is involved; section 5 shows the
storage functions if you want to keep results.

| Function | Endpoint | Request | Response | Errors |
|---|---|---|---|---|
| `steps.extract_cdp` | `POST /cdp` | multipart `file` (.pptx / .pdf / .docx) | `{indications: [names], output: <the cdp output>}` | 400 wrong suffix, 500 `ConfigError`, 502 `ExtractionError` |
| `steps.extract_ci` | `POST /ci` | multipart `file` + form field `indications` (the JSON list from `/cdp`) | the ci output | 422 bad `indications`, then as above |
| `steps.extract_protocol` | `POST /protocol` | multipart `file` + form field `indications` | the protocol output | as `ci` |
| `combine.combine` | `POST /combine` | JSON `{cdp, ci?, protocol?}`: the three outputs as returned above | the combined document | 422 when the bodies are not those outputs or both `ci` and `protocol` are missing |

The calling sequence, with curl:

```text
curl -F file=@"CDP.pptx" http://localhost:8000/cdp > cdp.json
curl -F file=@"CI.pdf" -F indications='["indication A", "indication B"]' http://localhost:8000/ci > ci.json
curl -F file=@"PCS.docx" -F indications='["indication A", "indication B"]' http://localhost:8000/protocol > protocol.json
python -c "import json; print(json.dumps({'cdp': json.load(open('cdp.json'))['output'], 'ci': json.load(open('ci.json')), 'protocol': json.load(open('protocol.json'))}))" > body.json
curl -H "Content-Type: application/json" --data @body.json http://localhost:8000/combine > combined.json
```

`/ci` and `/protocol` are independent once `/cdp` has answered, so a client can call them at the same time. The
routes are plain `def` functions, so FastAPI runs each in a worker thread and the event loop stays free while a
run waits on the gateway. The module below implements the table (run with
`python -m uvicorn example_api:app --port 8000` from the project folder; it was exercised end to end with the
three extraction functions stubbed by the synthetic outputs in `testing/`):

```python
"""structx behind FastAPI: one endpoint per steps.py function, stateless. Run from the project folder:
    python -m uvicorn example_api:app --port 8000
The client keeps the pieces between calls: the indication names from /cdp go into /ci and /protocol, and the three
outputs go into /combine. Nothing is stored here (section 5 shows the database functions if you want that)."""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

from fastapi import Body, FastAPI, File, Form, HTTPException, UploadFile

from structx.model.llm import ExtractionError
from structx.settings.config import ConfigError, StructConfig
from structx.settings.spec import load_spec
from structx.source.source import SUFFIXES
from structx.steps import extract_cdp, extract_ci, extract_protocol
from structx.join.combine import combine

GATEWAY = StructConfig.from_env()                                   # fails closed at import: no key, no server
ALIASES = {**load_spec("protocol").aliases, **load_spec("ci").aliases}
app = FastAPI(title="structx steps")


def saved(upload: UploadFile) -> tuple[Path, Path]:
    """The upload on disk in a fresh temporary folder (every step takes a path); the caller removes the folder."""
    name = Path(upload.filename or "upload").name
    if Path(name).suffix.lower() not in SUFFIXES:
        raise HTTPException(400, f"the file must be one of {', '.join(SUFFIXES)}")
    folder = Path(tempfile.mkdtemp(prefix="structx-"))
    path = folder / name
    path.write_bytes(upload.file.read())
    return folder, path


def run(func, upload: UploadFile, *args):
    """One step on one upload, its errors as HTTP statuses: ConfigError 500 (deployment), ExtractionError 502 (the run)."""
    folder, path = saved(upload)
    try:
        return func(path, *args, cfg=GATEWAY, write_files=False)
    except ConfigError as exc:
        raise HTTPException(500, str(exc))
    except ExtractionError as exc:
        raise HTTPException(502, str(exc))
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def names_of(indications: str) -> list[str]:
    """The `indications` form field: a JSON list of names, as /cdp returned them."""
    try:
        names = json.loads(indications)
    except ValueError:
        names = None
    if not isinstance(names, list) or not names or any(not isinstance(n, str) or not n.strip() for n in names):
        raise HTTPException(422, "indications must be a non-empty JSON list of names (the `indications` of /cdp)")
    return names


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/cdp")                                                    # steps.extract_cdp
def post_cdp(file: UploadFile = File()) -> dict:
    cdp = run(extract_cdp, file)                                      # CdpExtraction(output, indications)
    return {"indications": cdp.indications, "output": cdp.output}


@app.post("/ci")                                                     # steps.extract_ci
def post_ci(file: UploadFile = File(), indications: str = Form()) -> dict:
    return run(extract_ci, file, names_of(indications))


@app.post("/protocol")                                               # steps.extract_protocol
def post_protocol(file: UploadFile = File(), indications: str = Form()) -> dict:
    return run(extract_protocol, file, names_of(indications))


@app.post("/combine")                                                # combine.combine (pure code, no gateway call)
def post_combine(body: dict = Body()) -> dict:
    cdp, ci, protocol = body.get("cdp"), body.get("ci"), body.get("protocol")
    if not isinstance(cdp, dict) or not (isinstance(ci, dict) or isinstance(protocol, dict)):
        raise HTTPException(422, "body: {cdp: <the /cdp output>, ci: <the /ci output>?, protocol: <the /protocol output>?}, at least one of ci / protocol")
    try:
        return combine(cdp, ci if isinstance(ci, dict) else None, protocol if isinstance(protocol, dict) else None, aliases=ALIASES)
    except (ExtractionError, KeyError, TypeError, ValueError) as exc:
        raise HTTPException(422, f"not the outputs of /cdp, /ci and /protocol: {exc}")
```

What a real service adds around this: authentication and a size limit on uploads, a request log without
document content, and a rate limit per caller, since every call sends a whole document to the gateway. The
reference implementation in `api/` has those, plus the stateful parts (assets, documents and their uploaders,
the database, user edits of the values, an audit log): `api/routes_raw.py` is its version of the three
single-step routes.

## 7. What to keep in mind

- **Secrets.** Only the environment carries the key. The run log and every `repr` redact it. Never log `StructConfig` fields or the gateway's error bodies verbatim.
- **Data classification.** The entire file goes to the gateway, and the outputs quote it. Apply the same classification rules to your API's storage and logs as to the source documents.
- **Determinism.** Two runs on the same deck can differ in wording and confidence. Store the run you accepted; do not assume a re-run reproduces it.
- **Specs are the contract.** `structx/specs/*.json` define the fields, the prompt text and the database columns. Changing a spec changes the JSON schema, the tables and the catalogue; regenerate `docs/schema.sql`, recreate the database and rerun `testing/test_specs.py`, `test_combine.py`, `test_database.py`.
- **Offline tests.** `testing/test_steps.py` and `testing/test_api.py` show how to stub the gateway (`extract_output` is monkeypatched) so your own API tests need no network.
