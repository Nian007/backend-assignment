from datetime import datetime, timezone
from sqlalchemy.orm import Session
from app.models.canonical import Household


def upsert(
    db: Session,
    source_id: str,
    name: str,
    primary_advisor: str | None,
    risk_profile: str | None,
    service_tier: str | None,
    status: str,
    updated_at: datetime,
    deleted_at: datetime | None,
) -> tuple[Household, bool]:
    """Upsert by source_id. Returns (household, was_created)."""
    now = datetime.now(timezone.utc)
    existing = db.query(Household).filter_by(source_id=source_id).first()
    if existing:
        existing.name = name
        existing.primary_advisor = primary_advisor
        existing.risk_profile = risk_profile
        existing.service_tier = service_tier
        existing.status = status
        existing.updated_at = updated_at
        existing.deleted_at = deleted_at
        return existing, False
    h = Household(
        source_id=source_id,
        name=name,
        primary_advisor=primary_advisor,
        risk_profile=risk_profile,
        service_tier=service_tier,
        status=status,
        ingested_at=now,
        updated_at=updated_at,
        deleted_at=deleted_at,
    )
    db.add(h)
    return h, True


def get_by_source_id(db: Session, source_id: str) -> Household | None:
    return db.query(Household).filter_by(source_id=source_id).first()


def get_all_active(db: Session) -> list[Household]:
    return db.query(Household).filter(Household.deleted_at.is_(None)).all()
