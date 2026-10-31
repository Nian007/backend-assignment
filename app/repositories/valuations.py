from datetime import datetime, timezone, date
from sqlalchemy.orm import Session
from app.models.canonical import Valuation


def insert(
    db: Session,
    account_id: str,
    drop_id: str,
    as_of_date: date,
    total_market_value: str,
    cash_balance: str,
    currency: str,
) -> Valuation:
    now = datetime.now(timezone.utc)
    v = Valuation(
        account_id=account_id,
        drop_id=drop_id,
        as_of_date=as_of_date,
        total_market_value=total_market_value,
        cash_balance=cash_balance,
        currency=currency,
        ingested_at=now,
    )
    db.add(v)
    return v


def get_for_drop(db: Session, drop_id: str) -> list[Valuation]:
    return db.query(Valuation).filter_by(drop_id=drop_id).all()


def get_latest_for_account(db: Session, account_id: str) -> Valuation | None:
    """Returns the valuation from the most recent effective drop for an account."""
    return (
        db.query(Valuation)
        .filter_by(account_id=account_id)
        .order_by(Valuation.as_of_date.desc())
        .first()
    )
