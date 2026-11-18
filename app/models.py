"""Database tables.

Columns the API filters or authorizes on (ids, asset_id, role, status, email) are real columns.
The rest of each React object is stored as-is in a JSON `data` column, so the API returns exactly
the shapes in the React app's src/types/domain.ts. Columns can be split out of `data` later
(e.g. for reporting) without changing the API.
"""
from datetime import datetime, timezone

from sqlalchemy import JSON, BigInteger, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base

JsonDoc = JSON().with_variant(JSONB(), 'postgresql')


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Timestamps:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class User(Timestamps, Base):
    __tablename__ = 'users'
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)  # the React app sends the id
    name: Mapped[str] = mapped_column(String(60))
    email: Mapped[str] = mapped_column(String(80), unique=True)  # stored lower-case
    role: Mapped[str] = mapped_column(String(10))  # Admin | Editor | Viewer
    status: Mapped[str] = mapped_column(String(10))  # active | pending | inactive
    idp_subject: Mapped[str | None] = mapped_column(String(100), unique=True)  # Okta user id, linked on first sign-in
    invited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    data: Mapped[dict] = mapped_column(JsonDoc, default=dict)  # last, roleCode, permissions, asset


class Asset(Timestamps, Base):
    __tablename__ = 'assets'
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    asset_code: Mapped[str] = mapped_column(String(40), index=True)
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey('users.id', ondelete='SET NULL'))  # None = seeded asset
    # Workflow state, owned by the server: extracting -> inputs_review (or extraction_failed -> retry) -> active.
    status: Mapped[str] = mapped_column(String(20), default='active', server_default='active', index=True)
    data: Mapped[dict] = mapped_column(JsonDoc)  # the full Asset object incl. aia and assessments


class AssetAccess(Base):
    __tablename__ = 'user_asset_access'
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), primary_key=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey('assets.id', ondelete='CASCADE'), primary_key=True)


class DecisionTrace(Timestamps, Base):
    __tablename__ = 'decision_traces'
    asset_id: Mapped[str] = mapped_column(ForeignKey('assets.id', ondelete='CASCADE'), primary_key=True)
    data: Mapped[dict] = mapped_column(JsonDoc)


class Activity(Base):
    """The "recent activity" feed shown in the React app."""
    __tablename__ = 'activity'
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event: Mapped[str] = mapped_column(String(200))
    user_id: Mapped[int | None] = mapped_column(ForeignKey('users.id', ondelete='SET NULL'), index=True)
    user_name: Mapped[str] = mapped_column(String(60))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class AuditLog(Base):
    """Server-side audit of every write (who, what, which record). Not exposed through the API."""
    __tablename__ = 'audit_log'
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(60))
    target: Mapped[str] = mapped_column(String(320))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Catalog(Timestamps, Base):
    """Read-only documents loaded by DE: reference, policyVersions, policyDeltas, rules, policyMonitoring."""
    __tablename__ = 'catalog'
    key: Mapped[str] = mapped_column(String(40), primary_key=True)
    data: Mapped[dict | list] = mapped_column(JsonDoc)
