"""Assessment actions on one asset (`asset_id` is the asset code): run eligibility / impact, start extraction, save a scenario.

The eligibility and impact engines run in the browser (src/engine in the React app), so these endpoints only record
that the action happened, in structx.sqlite's audit log. structx has no table for scenarios: their content stays in the
browser, and only the fact that one was saved is recorded.
"""
import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends

from .. import structx_repo, structx_write
from ..schemas import ScenarioIn
from ..security import CurrentUser, Writer

router = APIRouter(prefix='/assets/{asset_id}', tags=['assessment'])
StructxRwConn = Annotated[sqlite3.Connection, Depends(structx_repo.get_structx_rw_conn)]


def _record(conn: sqlite3.Connection, user: CurrentUser, code: str, event: str) -> None:
    structx_write.record_event(conn, user, structx_repo.require_asset_code(conn, user, code, write=True), event)


@router.post('/eligibility/run', status_code=204)
def run_eligibility(asset_id: str, user: Writer, conn: StructxRwConn) -> None:
    _record(conn, user, asset_id, 'ran eligibility assessment')


@router.post('/impact/run', status_code=204)
def run_impact(asset_id: str, user: Writer, conn: StructxRwConn) -> None:
    _record(conn, user, asset_id, 'ran impact analysis')


@router.post('/extract', status_code=202)
def start_extraction(asset_id: str, user: Writer, conn: StructxRwConn) -> dict:
    """Extraction is run by the AI team's pipeline; this records the request."""
    _record(conn, user, asset_id, 'requested extraction')
    return {'status': 'requested'}


@router.post('/scenarios', status_code=201)
def save_scenario(asset_id: str, body: ScenarioIn, user: Writer, conn: StructxRwConn) -> dict:
    _record(conn, user, asset_id, 'saved scenario')
    return body.model_dump()
