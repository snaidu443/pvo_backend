"""Data Submission: create an asset from the set-up form and its documents in one request, then extract.

Nothing is saved while the user fills in the form. "Extract data with AI" sends everything at once:
    1. check the role, the set-up fields and every file   (nothing stored yet; any problem -> 4xx)
    2. store the files (local folder or S3)               (any failure -> stored files removed)
    3. one transaction: asset, access, documents, job, audit
    4. respond 202 {assetId, jobId}; the extraction job runs in the background
React then polls GET /assets/{id}/extract/{jobId}.
"""
import logging
import secrets
from collections import Counter
from datetime import date
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from .. import models
from ..config import get_settings
from ..database import get_session_factory
from ..extraction import ACTIVE_JOB_STATES, job_out, run_job
from ..files import DOC_LABELS, CheckedFile, FileRejected, FileTooBig, check_file, size_label
from ..permissions import audit, get_asset_for
from ..schemas import SubmissionIn
from ..security import AnyUser, DbSession, Writer
from ..storage import FileStorage, FileTooLarge, StorageError, StoredObject, get_storage
from .assets import _code_taken
from .documents import doc_out

router = APIRouter(tags=['submissions'])
log = logging.getLogger(__name__)
Storage = Annotated[FileStorage, Depends(get_storage)]
Sessions = Annotated[sessionmaker, Depends(get_session_factory)]
POLICIES = ('IRA', 'MFN', 'JCA', 'EU-GPL')


def _today() -> str:
    return date.today().strftime('%d %b %Y')


def _new_asset_id(db: DbSession) -> str:
    for _ in range(5):
        asset_id = f'PVO-{secrets.token_hex(4).upper()}'  # CSPRNG; not guessable, never chosen by the browser
        if db.get(models.Asset, asset_id) is None:
            return asset_id
    raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, 'Please try again.')


def _asset_data(asset_id: str, body: SubmissionIn, owner: str, role: str) -> dict:
    """The React app's Asset shape. Set-up answers go into `context`; React builds the workspace on open."""
    zeros = {p: 0 for p in POLICIES}
    context = {'modality': body.modality, 'jcaInsights': body.jcaInsights, 'regStrategy': body.regStrategy, 'euAccess': body.euAccess}
    return {
        'id': asset_id, 'assetCode': body.name, 'assetName': body.name, 'therapeuticArea': body.area or 'Other', 'stage': '',
        'jurisdictions': [], 'policyExposures': [], 'submissionStatus': 'In progress', 'submissionCompleteness': 0,
        'eligibilityStatus': 'Review pending', 'businessContextStatus': 'Draft', 'impactStatus': '—', 'owner': owner,
        'lastUpdatedAt': _today(), 'estimatedImpactLevel': '—', 'estimatedAtRisk': 0, 'estimatedOpportunity': 0,
        'recommendationStatus': '—', 'governanceAlerts': 0, 'summaryMetrics': {'gross': 0, 'net': 0, 'usRevenue': 0, 'euRevenue': 0},
        'eligibility': {p: 'Review pending' for p in POLICIES}, 'context': {k: v for k, v in context.items() if v is not None},
        'policyImpact': zeros, 'recommendationCount': 0, 'approvalCount': 0, 'createdByRole': role,
    }


def _parse(metadata: str) -> SubmissionIn:
    try:
        return SubmissionIn.model_validate_json(metadata)
    except ValidationError as exc:  # say where, without echoing values back
        errors = [{'loc': list(e.get('loc', [])), 'msg': e.get('msg', '')} for e in exc.errors()[:20]]
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            {'message': 'Some set-up values are missing or not valid.', 'errors': errors}) from exc


def _check_counts(body: SubmissionIn, files: list[UploadFile]) -> None:
    s = get_settings()
    if len(files) != len(body.documents):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            f'You sent {len(files)} file(s) but "documents" lists {len(body.documents)} entr'
                            f'{"y" if len(body.documents) == 1 else "ies"}. Send one file per entry, in the same order.')
    if len(files) > s.max_files_per_submission:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f'Upload at most {s.max_files_per_submission} files.')
    for doc_type, n in Counter(d.docType for d in body.documents).items():
        if n > s.max_files_per_type:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f'{DOC_LABELS[doc_type]}: at most {s.max_files_per_type} files.')


def _check_files(body: SubmissionIn, files: list[UploadFile]) -> list[CheckedFile]:
    s = get_settings()
    checked, seen, total = [], set(), 0
    for i, (meta, upload) in enumerate(zip(body.documents, files)):
        try:
            c = check_file(upload.file, upload.filename, meta.docType, s.max_upload_bytes)
        except FileTooBig as exc:
            raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, {'message': str(exc), 'file': i}) from exc
        except FileRejected as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, {'message': str(exc), 'file': i}) from exc
        if c.sha256 in seen:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, {'message': f'{c.original_name}: this file was added twice.', 'file': i})
        seen.add(c.sha256)
        total += c.size
        if total > s.max_submission_bytes:
            raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                                f'All files together must be {s.max_submission_bytes // (1024 * 1024)} MB or smaller.')
        checked.append(c)
    return checked


def _remove(storage: FileStorage, stored: list[StoredObject]) -> None:
    for obj in stored:
        try:
            storage.delete(obj.key)
        except Exception:  # noqa: BLE001 - best effort; the reconciliation job reports leftovers
            pass


@router.post('/assets/submissions', status_code=202)
def submit(
    background: BackgroundTasks, user: Writer, db: DbSession, storage: Storage, sessions: Sessions,
    metadata: Annotated[str, Form(max_length=20_000)],
    files: Annotated[list[UploadFile], File()],
) -> dict:
    """multipart/form-data: `metadata` (JSON, see SubmissionIn) and one `files` part per document, in order."""
    # 1. Check everything before storing anything.
    body = _parse(metadata)
    _check_counts(body, files)
    if _code_taken(db, body.name):
        raise HTTPException(status.HTTP_409_CONFLICT, 'An asset with this name already exists.')
    checked = _check_files(body, files)
    asset_id = _new_asset_id(db)

    # 2. Store the files.
    stored: list[StoredObject] = []
    try:
        for c, upload in zip(checked, files):
            stored.append(storage.save(upload.file, asset_id=asset_id, ext=c.ext, content_type=c.content_type,
                                       max_bytes=get_settings().max_upload_bytes))
    except FileTooLarge as exc:
        _remove(storage, stored)
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, 'Files must be 50 MB or smaller.') from exc
    except (StorageError, OSError) as exc:
        log.exception('Submission files could not be stored (asset %s)', asset_id)  # server log only
        _remove(storage, stored)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, 'The files could not be stored. Please try again.') from exc

    # 3. One transaction for every row.
    today, seq = _today(), Counter()
    try:
        data = _asset_data(asset_id, body, user.name, user.role)
        db.add(models.Asset(id=asset_id, asset_code=body.name, created_by_user_id=user.id, status='extracting', data=data))
        db.flush()
        db.add(models.AssetAccess(user_id=user.id, asset_id=asset_id))
        docs = []
        for meta, c, obj in zip(body.documents, checked, stored):
            seq[meta.docType] += 1
            docs.append(models.Document(
                asset_id=asset_id, doc_type=meta.docType, seq=seq[meta.docType], original_name=c.original_name,
                content_type=c.content_type, storage_backend=storage.backend, storage_key=obj.key,
                storage_version_id=obj.version_id, size_bytes=obj.size, sha256=c.sha256, scan_status='pending_scan',
                uploaded_by_user_id=user.id,
                data={'name': c.original_name, 'size': size_label(obj.size), 'type': meta.docType, 'scope': 'Program-wide',
                      'version': 'Latest', 'tag': '', 'pages': '—', 'added': today}))
        db.add_all(docs)
        job = models.ExtractionJob(asset_id=asset_id, status='queued', requested_by_user_id=user.id)
        db.add(job)
        db.flush()
        audit(db, user, 'asset.submit', f'asset:{asset_id} job:{job.id} documents:{len(docs)}')
        db.commit()
    except Exception:
        db.rollback()
        _remove(storage, stored)
        raise

    # 4. Extraction runs after the response is sent.
    background.add_task(run_job, sessions, storage, job.id)
    return {'assetId': asset_id, 'jobId': job.id, 'status': 'extracting', 'asset': {**data, 'workflowStatus': 'extracting'},
            'documents': [doc_out(d) for d in docs]}


def _job_for(db: DbSession, asset_id: str, job_id: int) -> models.ExtractionJob:
    job = db.get(models.ExtractionJob, job_id)
    if job is None or job.asset_id != asset_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'Extraction job not found.')
    return job


@router.get('/assets/{asset_id}/extract/latest')
def latest_job(asset_id: str, user: AnyUser, db: DbSession) -> dict:
    """The most recent extraction job (React uses it after a page refresh)."""
    get_asset_for(db, user, asset_id)
    job = db.scalar(select(models.ExtractionJob).where(models.ExtractionJob.asset_id == asset_id)
                    .order_by(models.ExtractionJob.id.desc()).limit(1))
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'No extraction has been started for this asset.')
    return job_out(job)


@router.get('/assets/{asset_id}/extract/{job_id}')
def get_job(asset_id: str, job_id: int, user: AnyUser, db: DbSession) -> dict:
    get_asset_for(db, user, asset_id)
    return job_out(_job_for(db, asset_id, job_id))


@router.post('/assets/{asset_id}/extract/retry', status_code=202)
def retry(asset_id: str, user: Writer, db: DbSession, storage: Storage, sessions: Sessions, background: BackgroundTasks) -> dict:
    """Start a new extraction after a failed one. The stored files are reused (no upload again)."""
    asset = get_asset_for(db, user, asset_id, write=True)
    if asset.status != 'extraction_failed':
        raise HTTPException(status.HTTP_409_CONFLICT, 'Only a failed extraction can be retried.')
    running = db.scalar(select(models.ExtractionJob.id).where(
        models.ExtractionJob.asset_id == asset_id, models.ExtractionJob.status.in_(ACTIVE_JOB_STATES)).limit(1))
    if running is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, 'An extraction is already running for this asset.')
    asset.status = 'extracting'
    job = models.ExtractionJob(asset_id=asset_id, status='queued', requested_by_user_id=user.id)
    db.add(job)
    db.flush()
    audit(db, user, 'extraction.retry', f'asset:{asset_id} job:{job.id}')
    db.commit()
    background.add_task(run_job, sessions, storage, job.id)
    return {'assetId': asset_id, 'jobId': job.id, 'status': 'extracting'}

