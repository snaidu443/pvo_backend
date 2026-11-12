"""Liveness / readiness for Kubernetes. No authentication and no details in the response."""
from fastapi import APIRouter, HTTPException, status
from sqlalchemy import text

from ..security import DbSession

router = APIRouter(prefix='/health', tags=['health'])


@router.get('/live')
def live() -> dict:
    return {'status': 'ok'}


@router.get('/ready')
def ready(db: DbSession) -> dict:
    try:
        db.execute(text('SELECT 1'))
    except Exception as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, 'Database unavailable.') from exc
    return {'status': 'ok'}
