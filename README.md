# PVO backend (FastAPI)

The REST API behind the Policy Value Optimizer React app (`Global_Policy_Re`). Every path, request and
response matches `src/api/endpoints.ts` and `src/types/domain.ts` in the React app. When the React app
is built with `PVO_DATA_SOURCE=api`, it talks to this service instead of its built-in mock data.

## 1. How the code is organised

```
pvo_backend/
├── app/
│   ├── main.py          # creates the app: routers, size limit, security headers, safe errors
│   ├── config.py        # settings from env vars (PVO_*) or .env; refuses unsafe config outside local
│   ├── database.py      # SQLAlchemy engine + one DB session per request
│   ├── models.py        # tables
│   ├── schemas.py       # request validation (mirrors the React types)
│   ├── security.py      # Okta token check + signed-in user + role checks
│   ├── permissions.py   # "may this user use this asset?" + audit helper
│   ├── storage.py       # uploaded files: local folder or private S3 bucket (same interface)
│   ├── files.py         # upload checks: allowed formats per slot, real file type, size, SHA-256
│   ├── extraction.py    # extraction jobs + malware-scan results
│   └── routers/         # one file per area of the React app
│       ├── auth.py        /auth/me, /auth/logout
│       ├── catalog.py     /reference, /policy-versions, /policy-deltas, /rules, /decision-traces, /policy-monitoring
│       ├── submissions.py /assets/submissions, /assets/{id}/extract/{jobId|latest|retry}  (docs/DATA_SUBMISSION_API.md)
│       ├── assets.py      /assets (list, create, save, delete)
│       ├── documents.py   /documents, /assets/{id}/documents (upload), /documents/{id}/download
│       ├── inputs.py      /fields, /questions (+ per-asset replace)
│       ├── assessment.py  /assets/{id}/eligibility/run, impact/run, extract, scenarios
│       ├── users.py       /users (+ invite)
│       ├── activity.py    /activity, /copilot/ask
│       └── health.py      /health/live, /health/ready  (for Kubernetes)
├── scripts/seed.py      # creates tables + loads the React mock JSON (synthetic data)
├── scripts/upgrade_local_db.py  # adds new columns to an existing local SQLite file (backs it up first)
├── tests/               # pytest: auth, permissions, main flows
├── Dockerfile
├── requirements.txt / requirements-dev.txt
└── .env.example
```

**A request goes through these layers:**
`router` → `security.get_current_user` (Okta token valid? user active?) → `permissions.get_asset_for`
(may this user use this asset?) → `schemas` (body valid?) → `models` (database) → JSON response in the React shape.

**Storage design (kept simple on purpose).** Columns the API filters or authorizes on are real columns:
ids, `asset_id`, `role`, `status`, `email`. The rest of each React object is stored as JSON (`data` column,
JSONB on Postgres/Lakebase). So the API returns exactly what the React app expects, and DE can split
more columns out later (for example for reporting) without changing the API.

| Table | Holds |
|---|---|
| `users`, `user_asset_access` | users, roles, and which assets each user may see |
| `assets` | the Asset object, including the assessment workspace (`aia`, incl. 15-year `revenue`) |
| `documents`, `extracted_fields`, `judgment_questions`, `decision_traces`, `saved_scenarios` | per-asset data (`documents` also holds where each file is stored and its scan status) |
| `extraction_jobs` | one row per "Extract data with AI" run (queued → waiting_for_scan → running → done / failed) |
| `assessment_runs` | eligibility / impact / extraction requests |
| `activity` | the recent-activity feed shown in the UI |
| `audit_log` | server-side record of every write (who, what, which id) |
| `catalog` | read-only data loaded by DE: reference, policyVersions, policyDeltas, rules, policyMonitoring |

## 2. Security rules built in

- **Sign-in is Okta only.** Every `/api` route needs a valid Okta access token. The token's signature
  is checked against Okta's keys (JWKS, RS256 only), along with issuer, audience, expiry and the
  `pvo.api` scope. The API never handles passwords.
- **Roles come from the PVO database.** Okta proves who the user is. The `users` table decides their
  role, and an unknown or inactive user gets 403. The first sign-in links the Okta account to the user
  an Admin created, matched by email.
- **Per-asset access on every request.** Admin sees all assets; everyone else only sees assets granted
  in `user_asset_access`. A user without access gets 404, the same as for an asset that doesn't exist,
  so asset ids can't be probed.
- **Viewers are read-only** (403 on any write). Editors can invite only Editors and Viewers, and only to
  their own assets. Admins can't lock themselves out.
- **Policy refresh monitoring is Admin-only.** Other roles get an empty result (see the note in `catalog.py`).
- **Every request body is validated.** Unknown keys are rejected and every string has a maximum length.
  Requests are limited to 2 MB of JSON and 50 MB for uploads.
- **Uploads are checked before saving.** The file type must be on the allowlist and the size is checked
  while the file streams in. Files are stored under random names, so a crafted name can't escape the
  folder, and they are marked `pending_scan` until the malware scan clears them.
- **Database queries are parameterised** through SQLAlchemy only; SQL is never built from strings.
- **Errors never expose internals:** no stack traces and no echoed input. Every response carries an
  `X-Request-ID` for support. Tokens and question text are never logged.
- **Unsafe configuration stops the app from starting** outside `local`: a missing Okta setting, CORS
  `*`, or SQLite in prod.

## 3. Step by step: run it locally

> Use **Python 3.12 or 3.13**. The pinned packages may not have ready-made wheels for 3.14 yet.
> **Before installing:** run your dependency / licence scan on `requirements*.txt` (as agreed).

**Step 1. Create a virtual environment and install packages**
```powershell
cd "C:\Users\<you>\OneDrive - TRINITY PARTNERS LLC\Takeda\pvo_backend"
py -3.12 -m venv .venv
.venv\Scripts\activate
pip install -r requirements-dev.txt
```

**Step 2. Configure**
```powershell
copy .env.example .env
```
Fill in `PVO_OKTA_ISSUER` and `PVO_OKTA_AUDIENCE` from the Okta team. For a quick local start,
`PVO_DATABASE_URL` can stay as SQLite.

**Step 3. Create the tables and load the demo data** (synthetic data from the React app)
```powershell
python -m scripts.seed --mocks "..\Global_Policy_Re\src\mocks\data" --admin-email <your company email> --admin-name "<Your Name>"
```
This makes you an Admin. You're linked to your Okta account the first time you sign in.

**Step 4. Start the API**
```powershell
uvicorn app.main:app --reload --port 8000
```
- **Live API docs:** http://localhost:8000/docs (local only; switched off in other environments)
- **Health check:** http://localhost:8000/health/ready

**Step 5. Run the tests**
```powershell
pytest
```
The tests use an in-memory database and a locally generated key that stands in for Okta. No network or
Okta account is needed.

**Step 6. Run React and the API together (local dev sign-in, until Okta is ready)**

1. In `pvo_backend/.env`, set:
   ```
   PVO_ENVIRONMENT=local
   PVO_DEV_LOGIN=true
   ```
   The API refuses to start with `PVO_DEV_LOGIN=true` in any other environment. While it's on, Okta
   tokens are not accepted.
2. Start the API: `python -m uvicorn app.main:app --reload --port 8000`. The log shows a
   "LOCAL DEV SIGN-IN IS ON" warning.
3. In `Global_Policy_Re/.env.local`, set:
   ```
   PVO_DATA_SOURCE=api
   PVO_API_BASE_URL=http://localhost:3000/api
   ```
4. Start React: `npm run dev`. The dev server forwards `/api` to `http://localhost:8000`, so there's
   no CORS setup. Production works the same way behind CloudFront.
5. Open http://localhost:3000 and sign in with the **email** of a seeded user. No password is needed:
   - the `--admin-email` you passed to the seed, for Admin;
   - any active user email in `src/mocks/data/users.json`, for Editor or Viewer.

   The role and visible assets come from the API. The "View as" switch is hidden in API mode.

To go back to the built-in mock data, set `PVO_DATA_SOURCE=mock` (or remove the line) and restart `npm run dev`.

**When Okta is ready:** set `PVO_DEV_LOGIN=false` and fill in the Okta values. On the React side, replace
the dev sign-in in `authService.login` with the Okta redirect. Then delete `app/devauth.py`.

## 4. Step by step: Lakebase / Postgres and deployment

1. **Database URL:** set `PVO_DATABASE_URL=postgresql+psycopg://<user>@<host>:5432/<db>?sslmode=require`.
   It comes from AWS Secrets Manager through the Kubernetes Secret, never from git. The tables are
   created by `scripts/seed.py` for dev/test; prod gets migrations (section 5).
2. **Build the image:** `docker build -t pvo-api:<git-sha> .`, then scan it and push it to ECR.
3. **Deploy:** run it on EKS as described in the deployment notes:
   - 2+ pods behind the ALB under `/api`;
   - probes on `/health/live` and `/health/ready`;
   - `PVO_ENVIRONMENT=dev|test|prod`;
   - Okta values in the ConfigMap, the database URL in the Secret.
4. **Uploads:** set `PVO_STORAGE_BACKEND=s3` with the bucket, prefix and KMS key (see `docs/DATA_SUBMISSION_API.md`). Production refuses local storage.

## 5. What is not done yet (next steps)

| # | Item | Why / what to do |
|---|---|---|
| 1 | **React sign-in via Okta** | Today React's API mode uses the local-only dev sign-in (`/auth/dev-login`). Switch React to the Okta redirect and `/auth/me` (needs approval for the Okta SDK packages), then remove `app/devauth.py`. |
| 2 | **Database migrations** | Tables are created with `create_all` for dev/test. Add Alembic before prod (new package, needs approval). |
| 3 | **Lakebase login tokens** | If Lakebase uses short-lived OAuth tokens as passwords, add token refresh (`databricks-sdk`, needs approval). |
| 4 | **File storage + malware scan** | `S3FileStorage` is written and tested with a stand-in client. Still needed: the approved `boto3` in `requirements.txt`, the bucket / KMS key / IAM role, and the scan-event consumer that calls `apply_scan_result`. |
| 5 | **Okta assignment / invite emails** | `TODO(okta)` in `users.py`; agree the process with the Okta team. |
| 6 | **AI extraction and PolicyPilot AI** | `/extract` records the request; `/copilot/ask` answers the suggested questions from the knowledge base only. |
| 7 | **Engines on the server** | Eligibility and impact are calculated in the browser (React `src/engine`), so the run endpoints only record the request. Port the engines to Python if results must be server-verified. |
| 8 | **Ids created by the browser** | Users (`max+1`) and new assets (`NEW-nnn`) get their ids in React. Two people adding at the same moment get a 409 ("refresh and try again"). Better: let the server assign them (small React change). |
| 9 | **React: Viewer controls** | React lets Viewers change document labels and include/exclude indications, but the API makes Viewers read-only (403), so hide those controls for Viewers. (The draft-document timing in `extractData()` is fixed.) |
| 10 | **Monitoring pill for non-Admins** | Non-Admins get empty findings from `/policy-monitoring`, so their header pill shows "Policy content current". Decide with the client whether Editors and Viewers should see the count. |

## 6. Before production

- **Scans:** run SAST (e.g. Bandit / Semgrep), a dependency scan (pip-audit or company tool), a
  container image scan and DAST against the test environment.
- **Human review:** have the code reviewed by the backend lead and CyberSecurity, especially
  `security.py`, `permissions.py`, `documents.py` and `users.py`.
- **Okta and network:** confirm the Okta settings with Takeda's Okta team, and the network path to
  Databricks/Lakebase.
- **Data:** keep using synthetic data until the data classification for real Takeda data is approved.
