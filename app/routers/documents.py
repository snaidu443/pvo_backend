"""Source documents: list, upload one more file, edit details, download, remove."""
import logging
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import ValidationError
from sqlalchemy import select

from .. import models
from ..config import get_settings
from ..files import CONTENT_TYPES, DOC_TYPES, FileRejected, FileTooBig, check_file
from ..permissions import accessible_asset_ids, audit, get_asset_for
from ..schemas import DocumentIn
from ..security import AnyUser, CurrentUser, DbSession, Writer
from ..storage import FileStorage, FileTooLarge, StorageError, get_storage

router = APIRouter(tags=['documents'])
log = logging.getLogger(__name__)

Storage = Annotated[FileStorage, Depends(get_storage)]
_CHUNK = 1024 * 1024
_ASCII_SAFE = set('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._- ()')


def doc_out(doc: models.Document) -> dict:
    """The React SourceDocument plus the server-owned scan status. Storage keys and paths are never returned."""
    return {**doc.data, 'id': doc.id, 'assetId': doc.asset_id, 'scanStatus': doc.scan_status}


def _document_for(db: DbSession, user: CurrentUser, doc_id: int, *, write: bool) -> models.Document:
    doc = db.get(models.Document, doc_id)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'Document not found.')
    get_asset_for(db, user, doc.asset_id, write=write)  # 403 / 404 if the user may not use its asset
    return doc


def _not_while_extracting(asset: models.Asset) -> None:
    if asset.status == 'extracting':
        raise HTTPException(status.HTTP_409_CONFLICT, 'Documents cannot change while extraction is running.')


@router.get('/documents')
def list_documents(user: AnyUser, db: DbSession) -> list[dict]:
    ids = accessible_asset_ids(db, user)
    stmt = select(models.Document).order_by(models.Document.id)
    if ids is not None:
        stmt = stmt.where(models.Document.asset_id.in_(sorted(ids)))
    return [doc_out(d) for d in db.scalars(stmt)]


@router.post('/assets/{asset_id}/documents', status_code=201)
def upload_document(
    asset_id: str, user: Writer, db: DbSession, storage: Storage,
    metadata: Annotated[str, Form(max_length=20_000)],
    file: Annotated[UploadFile | None, File()] = None,
) -> dict:
    """Add one document to an existing asset. multipart/form-data: `metadata` (SourceDocument JSON) and optionally `file`.

    With a file, `metadata.type` must be CI, Protocol, CDP or Forecast and the file is checked like Data Submission.
    """
    asset = get_asset_for(db, user, asset_id, write=True)
    _not_while_extracting(asset)
    try:
        record = DocumentIn.model_validate_json(metadata)
    except ValidationError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, 'Invalid document details.') from exc
    if record.assetId != asset_id:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, 'The document belongs to a different asset.')

    data = record.model_dump(exclude_none=True, exclude={'id', 'scanStatus'})
    doc = models.Document(asset_id=asset_id, data=data, scan_status='no_file', uploaded_by_user_id=user.id)
    key = None
    if file is not None:
        if record.type not in DOC_TYPES:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, 'Document type must be CI, Protocol, CDP or Forecast.')
        max_bytes = get_settings().max_upload_bytes
        try:
            checked = check_file(file.file, file.filename, record.type, max_bytes)
            if db.scalar(select(models.Document.id).where(models.Document.asset_id == asset_id,
                                                          models.Document.sha256 == checked.sha256)) is not None:
                raise FileRejected(f'{checked.original_name}: this file is already uploaded for this asset.')
            obj = storage.save(file.file, asset_id=asset_id, ext=checked.ext, content_type=checked.content_type, max_bytes=max_bytes)
        except (FileTooBig, FileTooLarge) as exc:
            raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, 'Files must be 50 MB or smaller.') from exc
        except FileRejected as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
        except (StorageError, OSError) as exc:
            log.exception('Document file could not be stored (asset %s)', asset_id)  # server log only
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, 'The file could not be stored. Please try again.') from exc
        key = obj.key
        n = db.scalar(select(models.Document.seq).where(models.Document.asset_id == asset_id, models.Document.doc_type == record.type)
                      .order_by(models.Document.seq.desc()).limit(1)) or 0
        # Uploaded files wait for the malware scan; nothing may open or download them before it marks them clean.
        doc.doc_type, doc.seq, doc.original_name, doc.content_type = record.type, n + 1, checked.original_name, checked.content_type
        doc.storage_backend, doc.storage_key, doc.storage_version_id = storage.backend, obj.key, obj.version_id
        doc.size_bytes, doc.sha256, doc.scan_status = obj.size, checked.sha256, 'pending_scan'
        doc.data = {**data, 'name': checked.original_name}
    db.add(doc)
    try:
        db.flush()
        audit(db, user, 'document.create', f'document:{doc.id} asset:{asset_id}')
        db.commit()
    except Exception:
        db.rollback()
        if key:
            storage.delete(key)
        raise
    return doc_out(doc)


@router.patch('/documents/{doc_id}')
def update_document(doc_id: int, body: DocumentIn, user: Writer, db: DbSession) -> dict:
    doc = _document_for(db, user, doc_id, write=True)
    if body.assetId != doc.asset_id:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, 'A document cannot move to another asset.')
    data = body.model_dump(exclude_none=True, exclude={'id', 'scanStatus'})
    if doc.storage_key:  # the stored file's name and type are fixed by the upload
        data.update(name=doc.data.get('name', data['name']), type=doc.doc_type or data['type'])
    doc.data = data
    audit(db, user, 'document.update', f'document:{doc_id}')
    db.commit()
    return doc_out(doc)


def _disposition(name: str) -> str:
    fallback = ''.join(ch if ch in _ASCII_SAFE else '_' for ch in name)[:150] or 'document'
    return f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{quote(name, safe="")}'


@router.get('/documents/{doc_id}/download')
def download_document(doc_id: int, user: AnyUser, db: DbSession, storage: Storage) -> StreamingResponse:
    """Stream the file through the API (no public or pre-signed links). Only files the scanner marked clean."""
    doc = _document_for(db, user, doc_id, write=False)
    if not doc.storage_key:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'This document has no stored file.')
    if doc.scan_status != 'clean':
        raise HTTPException(status.HTTP_409_CONFLICT, 'This file has not passed the malware scan.')
    try:
        stream = storage.open(doc.storage_key)
    except StorageError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, 'The file could not be read. Please try again.') from exc
    audit(db, user, 'document.download', f'document:{doc_id} asset:{doc.asset_id}')
    db.commit()

    def chunks():
        try:
            while chunk := stream.read(_CHUNK):
                yield chunk
        finally:
            stream.close()

    ext = (doc.storage_key.rsplit('.', 1) + [''])[1]
    media = CONTENT_TYPES.get(ext, 'application/octet-stream')  # from the server's allow-list, not the stored value
    return StreamingResponse(chunks(), media_type=media,
                             headers={'Content-Disposition': _disposition(doc.data.get('name') or 'document')})


@router.delete('/documents/{doc_id}', status_code=204)
def delete_document(doc_id: int, user: Writer, db: DbSession, storage: Storage) -> None:
    doc = _document_for(db, user, doc_id, write=True)
    _not_while_extracting(db.get(models.Asset, doc.asset_id))
    key = doc.storage_key
    db.delete(doc)
    audit(db, user, 'document.delete', f'document:{doc_id}')
    db.commit()
    if key:
        try:
            storage.delete(key)
        except StorageError:
            pass  # the row is gone; the reconciliation job removes the leftover object
