# Data Submission API

This is the backend for the **Data Submission** page: set up an asset, upload documents, then click **Extract data with AI**.

**Nothing is saved while the user fills in the form.** One request carries the set-up fields and every file. The server checks everything, stores the files, saves all rows in one transaction, and starts extraction in the background.

Every endpoint needs a signed-in user (Bearer token) and checks the user's rights to the specific asset.

| # | Method and path | Who | Purpose |
|---|---|---|---|
| 1 | `POST /api/assets/submissions` | Admin, Editor | Create the asset with its documents and start extraction |
| 2 | `GET /api/assets/{assetId}/extract/{jobId}` | Anyone with access to the asset | Status of one extraction job (React polls this) |
| 3 | `GET /api/assets/{assetId}/extract/latest` | Anyone with access to the asset | The most recent job (used after a page refresh) |
| 4 | `POST /api/assets/{assetId}/extract/retry` | Admin, Editor with access | Start again after a failed extraction, reusing the stored files |
| 5 | `GET /api/documents/{docId}/download` | Anyone with access to the asset | Download one file; only files the malware scan marked `clean` |
| 6 | `POST /api/assets/{assetId}/documents` | Admin, Editor with access | Add one more file to an existing asset (same checks) |
| 7 | `DELETE /api/documents/{docId}` | Admin, Editor with access | Remove a document and its stored file |

---

## 1. `POST /api/assets/submissions`

The request is `multipart/form-data` with these parts:

| Part | Content |
|---|---|
| `metadata` | JSON text, up to 20,000 characters (see below) |
| `files` | One part per file, in the **same order** as `metadata.documents` |

```json
{
  "name": "TEST-200",
  "area": "Oncology",
  "modality": "Small molecule",
  "jcaInsights": "No",
  "regStrategy": "Yes",
  "euAccess": null,
  "documents": [ { "docType": "CDP" }, { "docType": "CDP" }, { "docType": "Forecast" } ]
}
```

| Field | Rule |
|---|---|
| `name` | Required, 1–40 characters, no control characters. Must not already be used by another asset (409). |
| `area` | Optional. One of: Oncology, Rare hematology, Immunology, Cell therapy, Neuroscience, Metabolic disease, Other |
| `modality` | Required. One of: Small molecule, Biologic (other), Biologic – plasma-derived, CGT |
| `jcaInsights`, `regStrategy`, `euAccess` | Optional: `Yes` or `No` |
| `documents[].docType` | `CI`, `Protocol`, `CDP` or `Forecast`; one entry per file |

**File rules** (limits come from settings):

| Slot | Accepted formats |
|---|---|
| CI (Competitive Intelligence) | pdf, ppt, pptx |
| Protocol (Trial Protocol Concept Sheet) | pdf, doc, docx |
| CDP (Clinical Development Plan) | pdf, doc, docx, ppt, pptx |
| Forecast (Forecast Assumptions) | xls, xlsx, ppt, pptx |

Each file must also pass these checks:
- Its **first bytes must match** the extension: `%PDF-`, an Office ZIP with the right Office structure, or older Office formats. The type the browser sends is ignored.
- Macro-enabled Office ZIP files are rejected.
- It must not be empty, and must be **at most 50 MB**.

Limits across the request:
- at most **5 files per slot** and **20 per submission**;
- **200 MB in total**;
- the same file may not be added twice.

**Response `202 Accepted`:**
```json
{
  "assetId": "PVO-9F2C4E8A",
  "jobId": 3,
  "status": "extracting",
  "asset": { "id": "PVO-9F2C4E8A", "assetCode": "TEST-200", "context": { "modality": "Small molecule" }, "workflowStatus": "extracting" },
  "documents": [ { "id": 12, "assetId": "PVO-9F2C4E8A", "name": "CDP_FY27.pptx", "type": "CDP", "size": "4.0 MB", "scanStatus": "pending_scan" } ]
}
```
The `asset` and `documents` above are shortened. `asset` is the full React `Asset` object, and each document is a React `SourceDocument` plus `scanStatus`. Storage keys and paths are never returned.

**Errors.** Nothing is stored when any of these occur:

| Code | When | `detail` |
|---|---|---|
| 401 | Not signed in | message |
| 403 | Viewer role | message |
| 409 | Name already used | message |
| 413 | A file over 50 MB, or more than 200 MB in total | `{ "message", "file": <index> }` or message |
| 422 | Invalid set-up value, counts do not match, wrong format for the slot, content does not match, macros, duplicate, empty, too many files | `{ "message", "file": <index> }` or `{ "message", "errors": [{loc, msg}] }` |
| 503 | Storage unavailable (files already written are removed) | message |

## 2. `GET /api/assets/{assetId}/extract/{jobId}`, and 3. `.../extract/latest`

```json
{ "jobId": 3, "assetId": "PVO-9F2C4E8A", "status": "waiting_for_scan", "error": null, "fieldsFound": 0,
  "createdAt": "2026-11-10T23:10:00+00:00", "startedAt": null, "finishedAt": null }
```

| `status` | Meaning | `assets.status` (shown as `workflowStatus`) |
|---|---|---|
| `queued` | Just created | `extracting` |
| `waiting_for_scan` | Waiting for the malware scan of the files | `extracting` |
| `running` | Reading the clean files | `extracting` |
| `done` | Finished; the Inputs tab can open | `inputs_review` |
| `failed` | `error` holds a message that is safe to show | `extraction_failed` |

Poll every 2–3 seconds while the status is `queued`, `waiting_for_scan` or `running`. A job of another asset, or an asset without access, returns 404.

## 4. `POST /api/assets/{assetId}/extract/retry`

This is allowed only when the asset is `extraction_failed` and no job is running (otherwise 409). It returns `202 { assetId, jobId, status: "extracting" }`.

## 5. `GET /api/documents/{docId}/download`

The file is streamed through the API with `Content-Disposition: attachment`, and every download is recorded in `audit_log`. There are no public or pre-signed links.

| Code | When |
|---|---|
| 404 | No access to the asset, or the document has no stored file |
| 409 | The malware scan has not marked the file `clean` |

---

## What is stored, and where

| When | Table | Action |
|---|---|---|
| Click "Extract" | `assets` | INSERT: server-generated id `PVO-XXXXXXXX`, `status='extracting'`, set-up answers in `data.context` |
| | `user_asset_access` | INSERT: the creator |
| | `documents` | INSERT one row per file: `doc_type`, `seq`, `original_name`, `content_type`, `storage_backend`, `storage_key`, `storage_version_id`, `size_bytes`, `sha256`, `scan_status='pending_scan'`, `uploaded_by_user_id` |
| | `extraction_jobs` | INSERT: `status='queued'` |
| | `audit_log` | INSERT: `asset.submit` |
| Malware scan result | `documents` | UPDATE `scan_status`: `clean`, `infected` or `failed`. Infected files are deleted from storage at once. |
| Extraction | `extraction_jobs`, `assets` | UPDATE the job status and the asset status |

**The files themselves** are stored under `assets/<assetId>/<random-uuid>.<ext>`. The user's file name is never used as a path.

| `PVO_STORAGE_BACKEND` | Location |
|---|---|
| `local` (default) | `PVO_UPLOAD_DIR` (default `./uploads`). **Point it outside OneDrive**, for example `C:\PVOData\uploads`. |
| `s3` | `s3://<PVO_S3_BUCKET>/<PVO_S3_PREFIX>/assets/<assetId>/<uuid>.<ext>`, written with SSE-KMS and a SHA-256 checksum |

S3 settings (identifiers only; no secrets):
```
PVO_STORAGE_BACKEND=s3
PVO_S3_BUCKET=pvo-docs-dev
PVO_S3_PREFIX=pvo/dev
PVO_S3_KMS_KEY_ID=alias/pvo-docs-dev
PVO_S3_REGION=<region>
```
Credentials come from the AWS default chain: an IAM role in AWS, or `aws sso login` locally. **Never put access keys in `.env`.**

The app refuses to start in these cases:
- `s3` is set without a valid bucket or KMS key;
- `boto3` is not installed;
- production is not set to `s3`.

## Not done yet

| Item | What is needed |
|---|---|
| **boto3** | Needed only for `s3`. Run the dependency and licence scan, then add the approved version to `requirements.txt`. |
| **S3 bucket, KMS key, IAM role** | From the cloud team, with Block Public Access on, TLS-only and KMS-only policies, versioning and CloudTrail data events. The role needs only `s3:PutObject`, `s3:GetObject`, `s3:DeleteObject` and `s3:PutObjectTagging` on `<prefix>/assets/*`, plus `kms:GenerateDataKey` and `kms:Decrypt` on the one key. |
| **Malware-scan consumer** | GuardDuty Malware Protection for S3 → EventBridge → SQS → a worker that calls `app.extraction.apply_scan_result(...)`, then resumes the job it returns. Until a scanner reports, jobs stay `waiting_for_scan`. How local files get marked clean is a CyberSecurity decision; there is deliberately no bypass. |
| **AI extraction** | `app.extraction.no_extraction` is a placeholder that finds 0 fields; the React app still fills the inputs. A real extractor plugs into `run_job(..., extractor=...)`. |
| **Job runner** | Jobs run as FastAPI background tasks in the API process. For several pods or long jobs, move them to a queue worker. |
| **Reconciliation job** | Compare stored objects with `documents.storage_key`, then report and remove leftovers (for example after a crash between upload and commit). |
| **React wiring** | Multi-file slots, one `POST /assets/submissions` on Extract, then polling. |
