"""Session endpoints. Sign-in itself happens at Okta; there is no password endpoint."""
from fastapi import APIRouter

from ..permissions import audit
from ..security import AnyUser, DbSession

router = APIRouter(prefix='/auth', tags=['auth'])


@router.get('/me')
def me(user: AnyUser) -> dict:
    """The React app calls this after the Okta redirect to get the name and role (SessionUser)."""
    return {'name': user.name, 'role': user.role}


@router.post('/logout', status_code=204)
def logout(user: AnyUser, db: DbSession) -> None:
    # Tokens are stateless; the browser also signs out at Okta. This only records the event.
    audit(db, user, 'auth.logout', f'user:{user.id}')
    db.commit()
