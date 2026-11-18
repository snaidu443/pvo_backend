"""AI extraction of an asset's uploaded CDP / CI / Protocol documents. See app/data_submission/extraction.py."""
import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends

from .. import structx_repo
from ..data_submission import extraction
from ..security import AnyUser, Writer

router = APIRouter(tags=['extraction'])

StructxConn = Annotated[sqlite3.Connection, Depends(structx_repo.get_structx_conn)]
StructxRwConn = Annotated[sqlite3.Connection, Depends(structx_repo.get_structx_rw_conn)]


@router.post('/assets/{asset_id}/extraction', status_code=202)
def start_extraction(asset_id: str, user: Writer, conn: StructxRwConn, replace: bool = False) -> dict:
    """Start the extraction in the background. `asset_id` is the asset code. 409 when one is already running, when a file
    is missing, or when the asset already has values and `replace` is not true (a new run replaces them)."""
    asset = structx_repo.require_asset_code(conn, user, asset_id, write=True)
    return extraction.start(conn, user, asset, replace=replace)


@router.get('/assets/{asset_id}/extraction')
def extraction_state(asset_id: str, user: AnyUser, conn: StructxConn) -> dict:
    """The last run's state (idle, running, succeeded or failed) and what structx.sqlite knows about the asset's extraction."""
    asset = structx_repo.require_asset_code(conn, user, asset_id)
    return {'job': extraction.state(asset['name']), 'extraction': structx_repo.extraction_status(conn, asset)}
