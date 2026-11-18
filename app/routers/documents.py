"""Source documents: list and relabel. Both read from / write to structx.sqlite."""
import sqlite3
from typing import Annotated

from fastapi import APIRouter, Body, Depends, File, Form, UploadFile

from .. import structx_repo, structx_ui, structx_upload, structx_write
from ..security import AnyUser, Writer

router = APIRouter(tags=['documents'])

StructxConn = Annotated[sqlite3.Connection, Depends(structx_repo.get_structx_conn)]
StructxRwConn = Annotated[sqlite3.Connection, Depends(structx_repo.get_structx_rw_conn)]


@router.get('/documents')
def list_documents(user: AnyUser, conn: StructxConn) -> list[dict]:
    """Source documents of the assets the user may open. Read from structx.sqlite only (see app/structx_ui.py)."""
    return structx_ui.ui_documents(conn, user)


@router.get('/assets/{asset_id}/documents')
def list_asset_documents(asset_id: str, user: AnyUser, conn: StructxConn) -> list[dict]:
    """The documents of one asset, freshly read from structx.sqlite. `asset_id` is the asset code."""
    return structx_ui.ui_asset_documents(conn, structx_repo.require_asset_code(conn, user, asset_id))


@router.post('/assets/{asset_id}/documents', status_code=201)
def upload_document(asset_id: str, user: Writer, conn: StructxRwConn, type: Annotated[str, Form(max_length=30)],
                    file: Annotated[UploadFile, File()]) -> dict:
    """Add one source document (multipart: `type` = CI, Protocol, CDP or Forecast, and `file`). Saved in the upload folder
    and registered in structx.sqlite; see app/structx_upload.py for every check. `asset_id` is the asset code."""
    asset = structx_repo.require_asset_code(conn, user, asset_id, write=True)
    return structx_upload.add_document(conn, user, asset, type, file.filename, file.file)


@router.delete('/documents/{doc_id}', status_code=204)
def delete_document(doc_id: int, user: Writer, conn: StructxRwConn) -> None:
    """Remove a document: its record and labels, and its file in the upload folder. Values already extracted from it are kept.
    Lets a user swap a file (for example a Word protocol for its PDF) without deleting the asset."""
    structx_upload.remove_document(conn, user, doc_id)


@router.patch('/documents/{doc_id}')
def update_document(doc_id: int, body: Annotated[dict, Body()], user: Writer, conn: StructxRwConn) -> dict:
    """Relabel a document in structx.sqlite. It stores the file name, size and labels, so labels are the only change."""
    return structx_write.save_document_types(conn, user, doc_id, body)
