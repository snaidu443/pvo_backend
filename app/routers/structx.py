"""Read-only API over the AI team's structx.sqlite: assets, documents, indications, and the Inputs / Assumptions /
Submission values with their confidence and citations.

Every route requires a signed-in user and checks the user's right to the specific asset (404 when not permitted).
Writes are not here: extraction and user edits are written by the AI team's code.
"""
import sqlite3
from typing import Annotated, Literal

from fastapi import APIRouter, Depends

from .. import structx_repo as repo
from .. import structx_ui as ui
from ..security import AnyUser

router = APIRouter(prefix='/structx', tags=['structx'])
Conn = Annotated[sqlite3.Connection, Depends(repo.get_structx_conn)]
Screen = Literal['submission', 'indications', 'assumptions', 'inputs']  # anything else -> 422


@router.get('/lookups')
def get_lookups(user: AnyUser, conn: Conn) -> dict:
    """Options for the set-up dropdowns and every select of the input catalogue, from structx's own tables."""
    return ui.ui_lookups(conn)


@router.get('/assets')
def list_assets(user: AnyUser, conn: Conn) -> list[dict]:
    return repo.list_assets(conn, user)


@router.get('/assets/{asset_id}')
def get_asset(asset_id: int, user: AnyUser, conn: Conn) -> dict:
    asset = repo.require_asset(conn, user, asset_id)
    return {**repo.asset_out(asset), 'documents': repo.list_documents(conn, asset_id)}


@router.get('/assets/{asset_id}/indications')
def get_indications(asset_id: int, user: AnyUser, conn: Conn) -> dict:
    """The asset's indications (CDP first) plus the Indications-screen values."""
    repo.require_asset(conn, user, asset_id)
    return {'assetId': asset_id, 'indications': repo.list_indications(conn, asset_id),
            'items': repo.screen_items(conn, asset_id, 'indications')}


@router.get('/assets/{asset_id}/screens/{screen}')
def get_screen(asset_id: int, screen: Screen, user: AnyUser, conn: Conn) -> dict:
    repo.require_asset(conn, user, asset_id)
    return {'assetId': asset_id, 'screen': screen, 'items': repo.screen_items(conn, asset_id, screen)}


@router.get('/assets/{asset_id}/coverage')
def get_coverage(asset_id: int, user: AnyUser, conn: Conn) -> dict:
    repo.require_asset(conn, user, asset_id)
    return {'assetId': asset_id, 'screens': repo.coverage(conn, asset_id)}


@router.get('/assets/{asset_id}/extraction/status')
def get_extraction_status(asset_id: int, user: AnyUser, conn: Conn) -> dict:
    return repo.extraction_status(conn, repo.require_asset(conn, user, asset_id))
