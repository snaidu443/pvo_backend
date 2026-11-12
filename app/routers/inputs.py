"""Extracted fields (Inputs) and judgment questions (Assumptions).

Both work the same way, so one small set of helpers serves the two tables.
"""
from typing import Annotated

from fastapi import APIRouter, Body, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import delete, select

from .. import models
from ..permissions import accessible_asset_ids, audit, get_asset_for
from ..schemas import FieldIn, QuestionIn
from ..security import AnyUser, CurrentUser, DbSession, Writer

router = APIRouter(tags=['inputs'])

Model = type[models.Field] | type[models.Question]


def _out(row) -> dict:
    return {**row.data, 'id': row.id, 'assetId': row.asset_id}


def _list(db: DbSession, user: CurrentUser, model: Model) -> list[dict]:
    ids = accessible_asset_ids(db, user)
    stmt = select(model).order_by(model.asset_id, model.created_at)
    if ids is not None:
        stmt = stmt.where(model.asset_id.in_(sorted(ids)))
    return [_out(r) for r in db.scalars(stmt)]


def _replace(db: DbSession, user: CurrentUser, model: Model, asset_id: str, items: list[BaseModel]) -> list[dict]:
    """Replace every row of one asset with `items` (one transaction)."""
    get_asset_for(db, user, asset_id, write=True)
    ids = [i.id for i in items]
    if any(i.assetId != asset_id for i in items) or len(set(ids)) != len(ids):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, 'Every record must belong to this asset and have a unique id.')
    if ids and db.scalar(select(model.id).where(model.id.in_(ids), model.asset_id != asset_id).limit(1)):
        raise HTTPException(status.HTTP_409_CONFLICT, 'A record id is already used by another asset.')
    db.execute(delete(model).where(model.asset_id == asset_id))
    db.add_all(model(id=i.id, asset_id=asset_id, data=i.model_dump(exclude_none=True)) for i in items)
    audit(db, user, f'{model.__tablename__}.replace', f'asset:{asset_id} count:{len(items)}')
    db.commit()
    return [i.model_dump(exclude_none=True) for i in items]


def _upsert(db: DbSession, user: CurrentUser, model: Model, record_id: str, item: BaseModel) -> dict:
    """Create or update one row (the React mock does the same)."""
    if item.id != record_id:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, 'The id does not match the URL.')
    get_asset_for(db, user, item.assetId, write=True)
    row = db.get(model, record_id)
    if row is not None and row.asset_id != item.assetId:
        raise HTTPException(status.HTTP_409_CONFLICT, 'A record id is already used by another asset.')
    data = item.model_dump(exclude_none=True)
    if row is None:
        row = model(id=record_id, asset_id=item.assetId, data=data)
        db.add(row)
    else:
        row.data = data
    audit(db, user, f'{model.__tablename__}.save', f'{record_id}')
    db.commit()
    return _out(row)


# ---------------- fields (Inputs)

@router.get('/fields')
def list_fields(user: AnyUser, db: DbSession) -> list[dict]:
    return _list(db, user, models.Field)


@router.put('/assets/{asset_id}/fields')
def replace_fields(asset_id: str, body: Annotated[list[FieldIn], Body(max_length=3000)], user: Writer, db: DbSession) -> list[dict]:
    return _replace(db, user, models.Field, asset_id, body)


@router.patch('/fields/{field_id:path}')
def save_field(field_id: str, body: FieldIn, user: Writer, db: DbSession) -> dict:
    return _upsert(db, user, models.Field, field_id, body)


# ---------------- questions (Assumptions)

@router.get('/questions')
def list_questions(user: AnyUser, db: DbSession) -> list[dict]:
    return _list(db, user, models.Question)


@router.put('/assets/{asset_id}/questions')
def replace_questions(asset_id: str, body: Annotated[list[QuestionIn], Body(max_length=500)], user: Writer, db: DbSession) -> list[dict]:
    return _replace(db, user, models.Question, asset_id, body)


@router.patch('/questions/{question_id:path}')
def save_question(question_id: str, body: QuestionIn, user: Writer, db: DbSession) -> dict:
    return _upsert(db, user, models.Question, question_id, body)
