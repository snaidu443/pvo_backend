"""Assets: list, create (with starter records), save, delete."""
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, func, select

from .. import models
from ..permissions import accessible_asset_ids, audit, get_asset_for
from ..schemas import AssetIn, NewAssetIn
from ..security import AnyUser, DbSession, Writer
from ..storage import FileStorage, StorageError, get_storage

router = APIRouter(prefix='/assets', tags=['assets'])

# Tables whose rows belong to one asset and go when the asset is deleted.
_CHILDREN = (models.Document, models.Field, models.Question, models.DecisionTrace,
             models.SavedScenario, models.AssessmentRun, models.ExtractionJob, models.AssetAccess)


def _code_taken(db: DbSession, code: str, except_id: str | None = None) -> bool:
    stmt = select(models.Asset.id).where(func.lower(models.Asset.asset_code) == code.lower())
    if except_id:
        stmt = stmt.where(models.Asset.id != except_id)
    return db.scalar(stmt) is not None


def _ids_taken(db: DbSession, model, ids: list[str]) -> bool:
    return bool(ids) and db.scalar(select(model.id).where(model.id.in_(ids)).limit(1)) is not None


def _out(a: models.Asset) -> dict:
    return {**a.data, 'workflowStatus': a.status}  # status is server-owned; see models.Asset.status


@router.get('')
def list_assets(user: AnyUser, db: DbSession) -> list[dict]:
    ids = accessible_asset_ids(db, user)
    stmt = select(models.Asset).order_by(models.Asset.created_at, models.Asset.id)
    if ids is not None:
        stmt = stmt.where(models.Asset.id.in_(sorted(ids)))
    return [_out(a) for a in db.scalars(stmt)]


@router.post('', status_code=201)
def create_asset(body: NewAssetIn, user: Writer, db: DbSession) -> dict:
    a = body.asset
    if db.get(models.Asset, a.id) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, 'An asset with this id already exists. Refresh and try again.')
    if _code_taken(db, a.assetCode):
        raise HTTPException(status.HTTP_409_CONFLICT, 'An asset with this code already exists.')
    if any(f.assetId != a.id for f in body.fields) or any(q.assetId != a.id for q in body.questions):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, 'Fields and questions must belong to the new asset.')
    if _ids_taken(db, models.Field, [f.id for f in body.fields]) or _ids_taken(db, models.Question, [q.id for q in body.questions]):
        raise HTTPException(status.HTTP_409_CONFLICT, 'Some records already exist. Refresh and try again.')

    data = a.model_dump(exclude_none=True, exclude={'workflowStatus'})
    data['createdByRole'] = user.role  # server-owned: marks a user-added (deletable) asset
    db.add(models.Asset(id=a.id, asset_code=a.assetCode, created_by_user_id=user.id, data=data))
    db.flush()  # the asset row must exist before rows that reference it
    db.add(models.AssetAccess(user_id=user.id, asset_id=a.id))
    db.add_all(models.Field(id=f.id, asset_id=a.id, data=f.model_dump(exclude_none=True)) for f in body.fields)
    db.add_all(models.Question(id=q.id, asset_id=a.id, data=q.model_dump(exclude_none=True)) for q in body.questions)
    if body.trace is not None:
        db.add(models.DecisionTrace(asset_id=a.id, data={**body.trace, 'assetId': a.id}))
    audit(db, user, 'asset.create', f'asset:{a.id}')
    db.commit()
    return {**data, 'workflowStatus': 'active'}


@router.put('/{asset_id}')
def update_asset(asset_id: str, body: AssetIn, user: Writer, db: DbSession) -> dict:
    """Saves the whole asset, including its assessment workspace (aia) and assessment history."""
    row = get_asset_for(db, user, asset_id, write=True)
    if body.id != asset_id:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, 'The asset id does not match the URL.')
    if _code_taken(db, body.assetCode, except_id=asset_id):
        raise HTTPException(status.HTTP_409_CONFLICT, 'An asset with this code already exists.')
    data = body.model_dump(exclude_none=True, exclude={'workflowStatus'})
    data.pop('createdByRole', None)
    if row.data.get('createdByRole'):
        data['createdByRole'] = row.data['createdByRole']
    row.asset_code, row.data = body.assetCode, data
    audit(db, user, 'asset.update', f'asset:{asset_id}')
    db.commit()
    return _out(row)


@router.delete('/{asset_id}', status_code=204)
def delete_asset(asset_id: str, user: Writer, db: DbSession,
                 storage: Annotated[FileStorage, Depends(get_storage)]) -> None:
    row = get_asset_for(db, user, asset_id, write=True)
    if not row.data.get('createdByRole'):
        raise HTTPException(status.HTTP_403_FORBIDDEN, 'Only assets added in PVO can be deleted.')
    keys = [k for k in db.scalars(select(models.Document.storage_key).where(models.Document.asset_id == asset_id)) if k]
    for model in _CHILDREN:
        db.execute(delete(model).where(model.asset_id == asset_id))
    db.delete(row)
    audit(db, user, 'asset.delete', f'asset:{asset_id}')
    db.commit()
    for key in keys:  # files go only after the database change is committed
        try:
            storage.delete(key)
        except StorageError:
            pass  # rows are gone; the reconciliation job removes leftover objects
