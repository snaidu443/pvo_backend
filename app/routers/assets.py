"""Assets: list, read and save. All of it is read from / written to structx.sqlite."""
import sqlite3
from typing import Annotated

from fastapi import APIRouter, Body, Depends
from .. import structx_repo, structx_ui, structx_write
from ..security import AnyUser, Writer

router = APIRouter(prefix='/assets', tags=['assets'])
StructxConn = Annotated[sqlite3.Connection, Depends(structx_repo.get_structx_conn)]
StructxRwConn = Annotated[sqlite3.Connection, Depends(structx_repo.get_structx_rw_conn)]

@router.get('')
def list_assets(user: AnyUser, conn: StructxConn) -> list[dict]:
    """Assets for the Overview and Asset Impact Analysis tabs. Read from structx.sqlite only (see app/structx_ui.py)."""
    return structx_ui.ui_assets(conn, user)


@router.post('', status_code=201)
def create_asset(body: Annotated[dict, Body()], user: Writer, conn: StructxRwConn) -> dict:
    """Adds an empty asset to structx.sqlite (the app then saves its set-up with PUT). Files and extraction stay with the AI team."""
    return structx_write.create_asset(conn, user, body)


@router.get('/{asset_id}')
def get_asset(asset_id: str, user: AnyUser, conn: StructxConn) -> dict:
    """One asset, freshly read from structx.sqlite (the app calls this when an asset is selected). `asset_id` is the asset code."""
    return structx_ui.ui_asset(conn, structx_repo.require_asset_code(conn, user, asset_id))


@router.put('/{asset_id}')
def update_asset(asset_id: str, body: Annotated[dict, Body()], user: Writer, conn: StructxRwConn) -> dict:
    """Saves an asset edit into structx.sqlite (asset details, indications and input values; see app/structx_write.py).
    `asset_id` is the asset code. The app's own tables are not used."""
    return structx_write.save_asset(conn, user, structx_repo.require_asset_code(conn, user, asset_id, write=True), body)
