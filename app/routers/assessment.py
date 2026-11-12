"""Assessment actions on one asset: run eligibility / impact, start extraction, save a scenario.

The eligibility and impact engines run in the browser today (src/engine in the React app), so the
run endpoints record the request. When the engines move to Python they plug in here.
"""
from fastapi import APIRouter

from .. import models
from ..permissions import audit, get_asset_for
from ..schemas import ScenarioIn
from ..security import CurrentUser, DbSession, Writer

router = APIRouter(prefix='/assets/{asset_id}', tags=['assessment'])


def _record_run(db: DbSession, user: CurrentUser, asset_id: str, kind: str) -> None:
    get_asset_for(db, user, asset_id, write=True)
    db.add(models.AssessmentRun(asset_id=asset_id, kind=kind, requested_by_user_id=user.id))
    audit(db, user, f'run.{kind}', f'asset:{asset_id}')
    db.commit()


@router.post('/eligibility/run', status_code=204)
def run_eligibility(asset_id: str, user: Writer, db: DbSession) -> None:
    _record_run(db, user, asset_id, 'eligibility')


@router.post('/impact/run', status_code=204)
def run_impact(asset_id: str, user: Writer, db: DbSession) -> None:
    _record_run(db, user, asset_id, 'impact')


@router.post('/extract', status_code=202)
def start_extraction(asset_id: str, user: Writer, db: DbSession) -> dict:
    """Queues AI extraction (the pipeline is a later step). The React app saves the fields afterwards."""
    _record_run(db, user, asset_id, 'extract')
    return {'status': 'requested'}


@router.post('/scenarios', status_code=201)
def save_scenario(asset_id: str, body: ScenarioIn, user: Writer, db: DbSession) -> dict:
    get_asset_for(db, user, asset_id, write=True)
    data = body.model_dump()
    db.add(models.SavedScenario(asset_id=asset_id, created_by_user_id=user.id, data=data))
    audit(db, user, 'scenario.save', f'asset:{asset_id}')
    db.commit()
    return data
