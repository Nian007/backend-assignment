from datetime import datetime, timezone, date as date_type
from sqlalchemy.orm import Session
from app.models.canonical import CustodianDrop


def get_by_drop_id(db: Session, drop_id: str) -> CustodianDrop | None:
    return db.query(CustodianDrop).filter_by(drop_id=drop_id).first()


def create(
    db: Session,
    drop_id: str,
    custodian: str,
    as_of_date: date_type,
    delivered_at: datetime,
    snapshot_type: str,
    supersedes: str | None,
    payload_hash: str | None = None,
) -> CustodianDrop:
    now = datetime.now(timezone.utc)
    drop = CustodianDrop(
        drop_id=drop_id,
        custodian=custodian,
        as_of_date=as_of_date,
        delivered_at=delivered_at,
        snapshot_type=snapshot_type,
        supersedes=supersedes,
        status="pending",
        payload_hash=payload_hash,
        ingested_at=now,
    )
    db.add(drop)
    db.flush()
    return drop


def set_status(db: Session, drop: CustodianDrop, status: str) -> None:
    drop.status = status


def get_effective_drops(db: Session) -> dict[date_type, CustodianDrop]:
    """
    Return the effective (non-superseded) ingested drop for each as_of_date.

    Effective = the drop for a given date that no other drop supersedes.
    We consider both 'ingested' and 'pending' status (pending = currently being processed).
    """
    all_drops = (
        db.query(CustodianDrop)
        .filter(CustodianDrop.status.in_(["ingested", "pending"]))
        .all()
    )
    superseded_ids = {d.supersedes for d in all_drops if d.supersedes}

    effective: dict[date_type, CustodianDrop] = {}
    for drop in all_drops:
        if drop.drop_id in superseded_ids:
            continue
        existing = effective.get(drop.as_of_date)
        # Prefer the later delivery if two drops for the same date are both non-superseded
        if existing is None or drop.delivered_at > existing.delivered_at:
            effective[drop.as_of_date] = drop
    return effective


def get_all(db: Session) -> list[CustodianDrop]:
    return db.query(CustodianDrop).order_by(CustodianDrop.as_of_date).all()
