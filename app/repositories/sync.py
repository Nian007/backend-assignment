from datetime import datetime, timezone
from sqlalchemy.orm import Session
from app.models.canonical import SyncRun, SyncCheckpoint


def create_run(db: Session, source: str, run_type: str) -> SyncRun:
    run = SyncRun(
        source=source,
        run_type=run_type,
        status="running",
        started_at=datetime.now(timezone.utc),
        records_ingested=0,
        records_skipped=0,
        errors_count=0,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def complete_run(
    db: Session,
    run: SyncRun,
    records_ingested: int,
    records_skipped: int,
    errors: list[str],
    checkpoint_at: str | None,
) -> None:
    run.status = "success"
    run.completed_at = datetime.now(timezone.utc)
    run.records_ingested = records_ingested
    run.records_skipped = records_skipped
    run.errors_count = len(errors)
    run.error_detail = errors[:20] if errors else None
    run.checkpoint_at = checkpoint_at
    db.commit()


def fail_run(db: Session, run: SyncRun, errors: list[str]) -> None:
    run.status = "failed"
    run.completed_at = datetime.now(timezone.utc)
    run.errors_count = len(errors)
    run.error_detail = errors[:20] if errors else None
    db.commit()


def get_checkpoint(db: Session, source: str) -> str | None:
    chk = db.query(SyncCheckpoint).filter_by(source=source).first()
    return chk.last_successful_at if chk else None


def set_checkpoint(db: Session, source: str, value: str) -> None:
    now = datetime.now(timezone.utc)
    chk = db.query(SyncCheckpoint).filter_by(source=source).first()
    if chk:
        chk.last_successful_at = value
        chk.updated_at = now
    else:
        db.add(SyncCheckpoint(source=source, last_successful_at=value, updated_at=now))
    db.commit()


def get_recent_runs(db: Session, source: str | None = None, limit: int = 20) -> list[SyncRun]:
    q = db.query(SyncRun)
    if source:
        q = q.filter_by(source=source)
    return q.order_by(SyncRun.started_at.desc()).limit(limit).all()


def get_run_by_id(db: Session, run_id: str) -> SyncRun | None:
    return db.query(SyncRun).filter_by(id=run_id).first()


def has_any_successful_run(db: Session) -> bool:
    return db.query(SyncRun).filter_by(status="success").first() is not None
