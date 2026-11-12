"""Recent-activity feed and PolicyPilot."""
from fastapi import APIRouter
from sqlalchemy import select

from .. import models
from ..permissions import audit, get_asset_for
from ..schemas import ActivityIn, CopilotIn
from ..security import AnyUser, DbSession

router = APIRouter(tags=['activity'])

NOT_CONNECTED = ('PolicyPilot is not connected to the AI service yet. Try one of the suggested questions, '
                 'or ask the PVO team.')


@router.get('/activity')
def list_activity(user: AnyUser, db: DbSession) -> list[dict]:
    """Latest 10 entries. Admin sees everyone's; other users see their own."""
    stmt = select(models.Activity).order_by(models.Activity.created_at.desc(), models.Activity.id.desc()).limit(10)
    if not user.is_admin:
        stmt = stmt.where(models.Activity.user_id == user.id)
    return [{'event': a.event, 'user': a.user_name, 'time': a.created_at.strftime('%d %b %Y, %H:%M')}
            for a in db.scalars(stmt)]


@router.post('/activity', status_code=204)
def record_activity(body: ActivityIn, user: AnyUser, db: DbSession) -> None:
    db.add(models.Activity(event=body.event, user_id=user.id, user_name=user.name))
    db.commit()


@router.post('/copilot/ask')
def ask_copilot(body: CopilotIn, user: AnyUser, db: DbSession) -> dict:
    """Answers the suggested questions from the knowledge base in /reference. The AI service
    (guardrails → orchestrator → summarizer) replaces this later; the response shape stays."""
    if body.assetId:
        get_asset_for(db, user, body.assetId)
    ref = db.get(models.Catalog, 'reference')
    kb = (ref.data.get('copilot') or {}).get('knowledgeBase', []) if ref else []
    q = body.question.casefold()
    answer = next((e.get('answer') for e in kb if str(e.get('question', '')).strip().casefold() == q), None)
    audit(db, user, 'copilot.ask', f'asset:{body.assetId or "-"}')  # the question text is not stored
    db.commit()
    return {'text': answer or NOT_CONNECTED}
