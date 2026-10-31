from datetime import datetime, timezone
from sqlalchemy.orm import Session
from app.models.canonical import Finding


def create(
    db: Session,
    code: str,
    severity: str,
    message: str,
    entity_type: str | None = None,
    entity_id: str | None = None,
    source: str | None = None,
    drop_id: str | None = None,
    sync_run_id: str | None = None,
    expected_value: str | None = None,
    observed_value: str | None = None,
) -> Finding:
    f = Finding(
        code=code,
        severity=severity,
        message=message,
        entity_type=entity_type,
        entity_id=entity_id,
        source=source,
        drop_id=drop_id,
        sync_run_id=sync_run_id,
        expected_value=expected_value,
        observed_value=observed_value,
        created_at=datetime.now(timezone.utc),
    )
    db.add(f)
    return f


def get_for_entity(db: Session, entity_id: str) -> list[Finding]:
    return db.query(Finding).filter_by(entity_id=entity_id).all()


def get_for_sync_run(db: Session, sync_run_id: str) -> list[Finding]:
    return db.query(Finding).filter_by(sync_run_id=sync_run_id).all()


def get_for_household_accounts(db: Session, account_ids: list[str]) -> list[Finding]:
    if not account_ids:
        return []
    return (
        db.query(Finding)
        .filter(Finding.entity_id.in_(account_ids))
        .order_by(Finding.created_at.desc())
        .all()
    )


def get_active_for_household(db: Session, household_id: str, account_ids: list[str]) -> list[Finding]:
    """All findings related to a household: on the household itself or its accounts/persons."""
    ids = [household_id] + account_ids
    return (
        db.query(Finding)
        .filter(Finding.entity_id.in_(ids))
        .order_by(Finding.created_at.desc())
        .all()
    )
