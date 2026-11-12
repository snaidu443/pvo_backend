"""Read-only data: reference catalogues, policy versions / deltas, rules, decision traces, monitoring."""
from fastapi import APIRouter, HTTPException, Response, status
from sqlalchemy import select

from .. import models
from ..permissions import accessible_asset_ids
from ..security import AnyUser, DbSession

router = APIRouter(tags=['reference'])


def _catalog(db: DbSession, key: str):
    row = db.get(models.Catalog, key)
    if row is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, 'Reference data has not been loaded yet.')
    return row.data


@router.get('/reference')
def reference(user: AnyUser, db: DbSession, response: Response) -> dict:
    """Policies, recommendations, policy rules, IRA considerations, tour, PolicyPilot KB, model constants."""
    response.headers['Cache-Control'] = 'private, max-age=300'
    return _catalog(db, 'reference')


@router.get('/policy-versions')
def policy_versions(user: AnyUser, db: DbSession) -> list:
    return _catalog(db, 'policyVersions')


@router.get('/policy-deltas')
def policy_deltas(user: AnyUser, db: DbSession) -> list:
    return _catalog(db, 'policyDeltas')


@router.get('/rules')
def rules(user: AnyUser, db: DbSession) -> list:
    return _catalog(db, 'rules')


@router.get('/decision-traces')
def decision_traces(user: AnyUser, db: DbSession) -> dict:
    """{ assetId: DecisionTrace } for the assets the user can access."""
    ids = accessible_asset_ids(db, user)
    stmt = select(models.DecisionTrace)
    if ids is not None:
        stmt = stmt.where(models.DecisionTrace.asset_id.in_(sorted(ids)))
    return {t.asset_id: t.data for t in db.scalars(stmt)}


@router.get('/policy-monitoring')
def policy_monitoring(user: AnyUser, db: DbSession) -> dict:
    """Admin only. Other roles get an empty result (not 403) because the React app loads this with
    every sign-in and a failure would stop the whole workspace from loading."""
    data = _catalog(db, 'policyMonitoring')
    if user.is_admin:
        return data
    return {'baselineDate': data.get('baselineDate', ''), 'lastRefresh': data.get('lastRefresh', ''),
            'pipelines': [], 'documents': [], 'findings': []}
