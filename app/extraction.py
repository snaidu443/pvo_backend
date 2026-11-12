"""Extraction jobs ("Extract data with AI") and malware-scan results.

Job life cycle
    queued -> waiting_for_scan (files not scanned yet) -> running -> done   (asset: inputs_review)
                                                               \\-> failed (asset: extraction_failed; user can retry)

Only files the malware scanner marked `clean` are ever read. Until a scanner reports, jobs wait.

The AI extraction pipeline is a later step. `no_extraction` is the placeholder: it reads nothing and
finds no fields, and the React app fills the inputs as it does today. A real extractor plugs into
`run_job(..., extractor=...)` and returns the number of fields it saved.
"""
import logging
from collections.abc import Callable

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from . import models
from .models import utcnow
from .storage import FileStorage, StorageError

log = logging.getLogger(__name__)

Extractor = Callable[[Session, models.Asset, list[models.Document], FileStorage], int]
ACTIVE_JOB_STATES = ('queued', 'waiting_for_scan', 'running')

# Malware-scan results (GuardDuty Malware Protection for S3 tag values). Anything else counts as failed.
_SCAN_VERDICTS = {'NO_THREATS_FOUND': 'clean', 'THREATS_FOUND': 'infected'}


def no_extraction(db: Session, asset: models.Asset, docs: list[models.Document], storage: FileStorage) -> int:
    return 0


def job_out(job: models.ExtractionJob) -> dict:
    iso = lambda d: d.isoformat() if d else None  # noqa: E731
    return {'jobId': job.id, 'assetId': job.asset_id, 'status': job.status, 'error': job.error,
            'fieldsFound': job.fields_found, 'createdAt': iso(job.created_at), 'startedAt': iso(job.started_at),
            'finishedAt': iso(job.finished_at)}


def _audit(db: Session, job: models.ExtractionJob, action: str) -> None:
    db.add(models.AuditLog(user_id=job.requested_by_user_id, action=action, target=f'asset:{job.asset_id} job:{job.id}'))


def _claim(db: Session, job_id: int, from_states: tuple[str, ...], to_state: str, **values) -> bool:
    """Move a job to another state only if nobody else has (safe when two runners start at once)."""
    result = db.execute(update(models.ExtractionJob)
                        .where(models.ExtractionJob.id == job_id, models.ExtractionJob.status.in_(from_states))
                        .values(status=to_state, **values))
    db.commit()
    return result.rowcount == 1


def _fail(db: Session, job_id: int, message: str) -> None:
    job = db.get(models.ExtractionJob, job_id)
    asset = db.get(models.Asset, job.asset_id)
    job.status, job.error, job.finished_at = 'failed', message[:300], utcnow()
    if asset is not None:
        asset.status = 'extraction_failed'
    _audit(db, job, 'extraction.failed')
    db.commit()


def run_job(sessions: sessionmaker, storage: FileStorage, job_id: int, extractor: Extractor = no_extraction) -> None:
    """Runs after the HTTP response. Never raises: every outcome is recorded on the job."""
    with sessions() as db:
        try:
            job = db.get(models.ExtractionJob, job_id)
            if job is None or job.status not in ('queued', 'waiting_for_scan'):
                return
            docs = list(db.scalars(select(models.Document).where(
                models.Document.asset_id == job.asset_id, models.Document.storage_key.is_not(None))
                .order_by(models.Document.doc_type, models.Document.seq, models.Document.id)))
            if any(d.scan_status == 'pending_scan' for d in docs):
                _claim(db, job_id, ('queued',), 'waiting_for_scan')
                return
            clean = [d for d in docs if d.scan_status == 'clean']
            if not clean:
                _fail(db, job_id, 'No document passed the malware scan. Upload the files again.')
                return
            if not _claim(db, job_id, ('queued', 'waiting_for_scan'), 'running', started_at=utcnow()):
                return
            asset = db.get(models.Asset, job.asset_id)
            found = extractor(db, asset, clean, storage)
            db.refresh(job)
            job.status, job.fields_found, job.finished_at, job.error = 'done', int(found), utcnow(), None
            asset.status = 'inputs_review'
            _audit(db, job, 'extraction.done')
            db.commit()
        except StorageError:
            log.exception('Extraction job %s could not read its files', job_id)
            db.rollback()
            _fail(db, job_id, 'The documents could not be read. Try again.')
        except Exception:  # noqa: BLE001 - recorded on the job; details go to the server log only
            log.exception('Extraction job %s failed', job_id)
            db.rollback()
            _fail(db, job_id, 'Extraction failed. Try again.')


def apply_scan_result(db: Session, storage: FileStorage, storage_key: str, verdict: str) -> int | None:
    """Record the scanner's verdict for one stored file. Infected files are removed from storage at once.

    Called by the scan-event consumer (AWS: GuardDuty -> EventBridge -> SQS), never by a user request.
    Returns the id of an extraction job that can now continue, if any.
    """
    doc = db.scalar(select(models.Document).where(models.Document.storage_key == storage_key))
    if doc is None:
        log.warning('Scan result for an unknown storage key')  # the key itself is not logged
        return None
    status = _SCAN_VERDICTS.get(verdict, 'failed')  # fail closed: unknown verdicts are never "clean"
    doc.scan_status = status
    db.add(models.AuditLog(user_id=None, action=f'document.scan_{status}', target=f'document:{doc.id} asset:{doc.asset_id}'))
    db.commit()
    if status == 'infected':
        try:
            storage.delete(storage_key)
        except StorageError:
            log.exception('Could not remove infected document %s', doc.id)

    pending = db.scalar(select(models.Document.id).where(
        models.Document.asset_id == doc.asset_id, models.Document.scan_status == 'pending_scan').limit(1))
    if pending is not None:
        return None
    return db.scalar(select(models.ExtractionJob.id).where(
        models.ExtractionJob.asset_id == doc.asset_id, models.ExtractionJob.status == 'waiting_for_scan'))
