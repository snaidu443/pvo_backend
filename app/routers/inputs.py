"""Inputs and Assumptions starter records. structx.sqlite has none: the values are saved with the asset (PUT /assets/{code})."""
import sqlite3
from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, status

from .. import structx_repo as repo
from ..security import AnyUser, Writer

router = APIRouter(tags=['inputs'])

StructxConn = Annotated[sqlite3.Connection, Depends(repo.get_structx_conn)]


# ---------------- fields (Inputs)

@router.get('/fields')
def list_fields(user: AnyUser) -> list[dict]:
    """structx.sqlite has no counterpart of these starter fields: the values live in each asset's workspace (see structx_ui)."""
    return []


def _nothing_to_store(asset_id: str, user: Writer, conn: StructxConn, items: list) -> list:
    """structx.sqlite has no place for starter fields or questions (the values are saved with the asset, see PUT /assets/{code}).
    An empty list is accepted (there is nothing to save); anything else is refused so nothing is silently dropped."""
    repo.require_asset_code(conn, user, asset_id, write=True)
    if items:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, 'These records are not stored for this asset.')
    return []


@router.put('/assets/{asset_id}/fields')
def replace_fields(asset_id: str, body: Annotated[list[dict], Body(max_length=3000)], user: Writer, conn: StructxConn) -> list[dict]:
    return _nothing_to_store(asset_id, user, conn, body)


@router.patch('/fields/{field_id:path}')
def save_field(field_id: str, user: Writer) -> dict:
    raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, 'These records are not stored for this asset.')


# ---------------- questions (Assumptions)

@router.get('/questions')
def list_questions(user: AnyUser) -> list[dict]:
    """Assumptions come from the catalogue values in each asset's workspace (see structx_ui), not from separate questions."""
    return []


@router.put('/assets/{asset_id}/questions')
def replace_questions(asset_id: str, body: Annotated[list[dict], Body(max_length=500)], user: Writer, conn: StructxConn) -> list[dict]:
    return _nothing_to_store(asset_id, user, conn, body)


@router.patch('/questions/{question_id:path}')
def save_question(question_id: str, user: Writer) -> dict:
    raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, 'These records are not stored for this asset.')
