"""Per-asset authorization. Admin sees every asset; other users only the assets granted to them."""
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models
from .security import CurrentUser


def accessible_asset_ids(db: Session, user: CurrentUser) -> set[str] | None:
    """Asset ids the user may see, or None for "all" (Admin)."""
    if user.is_admin:
        return None
    return set(db.scalars(select(models.AssetAccess.asset_id).where(models.AssetAccess.user_id == user.id)))


def can_access(db: Session, user: CurrentUser, asset_id: str) -> bool:
    return user.is_admin or db.get(models.AssetAccess, (user.id, asset_id)) is not None


def get_asset_for(db: Session, user: CurrentUser, asset_id: str, *, write: bool = False) -> models.Asset:
    """The asset if the user may use it. Unknown and not-permitted assets both return 404 (no id probing)."""
    if write and not user.can_write:
        raise HTTPException(status.HTTP_403_FORBIDDEN, 'View only for your role.')
    asset = db.get(models.Asset, asset_id)
    if asset is None or not can_access(db, user, asset_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'Asset not found.')
    return asset


def audit(db: Session, user: CurrentUser, action: str, target: str) -> None:
    """Server-side audit row; added to the caller's transaction so it commits with the change."""
    db.add(models.AuditLog(user_id=user.id, action=action, target=target[:320]))
