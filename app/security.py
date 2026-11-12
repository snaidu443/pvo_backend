"""Okta access-token validation and the signed-in user.

Every API route depends on `get_current_user`. Anything unexpected (no token, bad signature,
wrong issuer / audience / scope, expired, unknown or inactive user) is denied: fail closed.
"""
import logging
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated, Any

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models
from .config import get_settings
from .database import get_db

log = logging.getLogger(__name__)
_bearer = HTTPBearer(auto_error=False)


def _unauthorized() -> HTTPException:
    return HTTPException(status.HTTP_401_UNAUTHORIZED, 'Your session has ended. Please sign in again.',
                         headers={'WWW-Authenticate': 'Bearer'})


class TokenVerifier:
    """Checks an Okta access token: RS256 signature (JWKS), issuer, audience, expiry, client id and scope."""

    def __init__(self, issuer: str, audience: str, client_id: str, required_scope: str, key_for: Callable[[str], Any]):
        self.issuer, self.audience, self.client_id, self.required_scope = issuer, audience, client_id, required_scope
        self._key_for = key_for

    def verify(self, token: str) -> dict[str, Any]:
        try:
            claims = jwt.decode(
                token, self._key_for(token), algorithms=['RS256'], audience=self.audience, issuer=self.issuer,
                leeway=60, options={'require': ['exp', 'iat', 'iss', 'aud', 'sub']})
        except jwt.PyJWTError as exc:
            log.info('Rejected access token: %s', type(exc).__name__)  # never log the token itself
            raise _unauthorized() from exc
        scopes = claims.get('scp') or []
        if isinstance(scopes, str):
            scopes = scopes.split()
        if self.client_id and claims.get('cid') != self.client_id:
            raise _unauthorized()
        if self.required_scope and self.required_scope not in scopes:
            raise _unauthorized()
        return claims


@lru_cache
def _okta_verifier() -> TokenVerifier:
    s = get_settings()
    jwks = jwt.PyJWKClient(s.jwks_url, cache_keys=True, lifespan=3600)
    return TokenVerifier(s.okta_issuer, s.okta_audience, s.okta_client_id, s.okta_required_scope,
                         lambda token: jwks.get_signing_key_from_jwt(token).key)


def get_verifier() -> TokenVerifier:
    """Dependency (tests replace it with a verifier that uses a local test key)."""
    s = get_settings()
    if s.dev_login:
        if s.environment != 'local':  # also checked at start-up; never trust dev tokens elsewhere
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, 'Sign-in is not configured.')
        from .devauth import dev_verifier
        return dev_verifier()
    if not s.okta_issuer or not s.okta_audience:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, 'Sign-in is not configured.')
    return _okta_verifier()


@dataclass(frozen=True)
class CurrentUser:
    id: int
    name: str
    email: str
    role: str

    @property
    def is_admin(self) -> bool:
        return self.role == 'Admin'

    @property
    def can_write(self) -> bool:
        return self.role in ('Admin', 'Editor')


def get_current_user(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    verifier: Annotated[TokenVerifier, Depends(get_verifier)],
    db: Annotated[Session, Depends(get_db)],
) -> CurrentUser:
    if creds is None:
        raise _unauthorized()
    claims = verifier.verify(creds.credentials)
    subject = str(claims.get('uid') or claims['sub'])  # Okta 'uid' never changes; 'sub' is the login

    if claims['iss'] == 'pvo-local-dev':  # local dev sign-in: match by email, never link an Okta account
        user = db.scalar(select(models.User).where(models.User.email == str(claims['sub']).lower()))
    else:
        user = db.scalar(select(models.User).where(models.User.idp_subject == subject))
    if user is None and claims['iss'] != 'pvo-local-dev':
        # First sign-in: link the Okta account to the user an Admin created (matched by email).
        email = str(claims.get('email') or claims['sub']).strip().lower()
        user = db.scalar(select(models.User).where(models.User.email == email, models.User.idp_subject.is_(None)))
        if user is not None:
            user.idp_subject = subject
    if user is None or user.status == 'inactive':
        raise HTTPException(status.HTTP_403_FORBIDDEN, 'Your account does not have access to PVO.')
    if user.status == 'pending':  # an invited user signing in for the first time
        user.status = 'active'
        user.data = {**user.data, 'active': True}
    if db.dirty:
        db.commit()
    return CurrentUser(id=user.id, name=user.name, email=user.email, role=user.role)


def require_writer(user: Annotated[CurrentUser, Depends(get_current_user)]) -> CurrentUser:
    if not user.can_write:
        raise HTTPException(status.HTTP_403_FORBIDDEN, 'View only for your role.')
    return user


def require_admin(user: Annotated[CurrentUser, Depends(get_current_user)]) -> CurrentUser:
    if not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, 'Only Admin can do this.')
    return user


DbSession = Annotated[Session, Depends(get_db)]
AnyUser = Annotated[CurrentUser, Depends(get_current_user)]
Writer = Annotated[CurrentUser, Depends(require_writer)]
Admin = Annotated[CurrentUser, Depends(require_admin)]
