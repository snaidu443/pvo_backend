"""LOCAL DEVELOPMENT ONLY: user name + password sign-in until the Okta app exists.

Switched on with PVO_DEV_LOGIN=true and only when PVO_ENVIRONMENT=local (config.check() refuses to
start otherwise). The password is one shared local test password from PVO_DEV_PASSWORD in .env. It issues short-lived RS256 tokens that go through the same TokenVerifier and the
same user / role / asset checks as Okta tokens. The signing key is generated in memory at start-up,
so tokens stop working whenever the server restarts. Remove this module once Okta is connected.
"""
import hmac
import time
from functools import lru_cache
from typing import Annotated

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, StringConstraints
from sqlalchemy import select

from . import models
from .config import get_settings
from .security import DbSession, TokenVerifier

DEV_ISSUER = DEV_AUDIENCE = 'pvo-local-dev'
TOKEN_SECONDS = 3600
_LOOPBACK = {'127.0.0.1', '::1', 'localhost', 'testclient'}


@lru_cache
def _key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def dev_verifier() -> TokenVerifier:
    public = _key().public_key()
    return TokenVerifier(DEV_ISSUER, DEV_AUDIENCE, '', 'pvo.api', lambda _token: public)


def issue_token(email: str) -> str:
    now = int(time.time())
    claims = {'iss': DEV_ISSUER, 'aud': DEV_AUDIENCE, 'sub': email, 'iat': now, 'exp': now + TOKEN_SECONDS, 'scp': ['pvo.api']}
    return jwt.encode(claims, _key(), algorithm='RS256')


class DevLoginIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    username: Annotated[str, StringConstraints(strip_whitespace=True, to_lower=True, min_length=1, max_length=80)]
    password: Annotated[str, StringConstraints(min_length=1, max_length=128)]


router = APIRouter(prefix='/auth', tags=['auth (local dev only)'])


def _find_user(db: DbSession, username: str) -> models.User | None:
    """`admin` matches admin@example.test; a full email matches exactly. Ambiguous names match nobody."""
    if '@' in username:
        return db.scalar(select(models.User).where(models.User.email == username))
    # autoescape: '%' or '_' typed in the user name are matched literally, not as wildcards
    matches = list(db.scalars(select(models.User).where(models.User.email.startswith(f'{username}@', autoescape=True))))
    return matches[0] if len(matches) == 1 else None


@router.post('/dev-login')
def dev_login(body: DevLoginIn, request: Request, db: DbSession) -> dict:
    """Returns a token for an active PVO user when the shared local test password matches.
    Refuses anything not from this machine. One generic error, so it does not reveal which part was wrong."""
    if request.client is None or request.client.host not in _LOOPBACK:
        raise HTTPException(status.HTTP_403_FORBIDDEN, 'Development sign-in only works on this computer.')
    expected = get_settings().dev_password.get_secret_value().encode()
    password_ok = bool(expected) and hmac.compare_digest(body.password.encode(), expected)
    user = _find_user(db, body.username)
    if not password_ok or user is None or user.status == 'inactive':
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, 'Invalid user name or password.')
    return {'access_token': issue_token(user.email), 'user': {'name': user.name, 'role': user.role}}
