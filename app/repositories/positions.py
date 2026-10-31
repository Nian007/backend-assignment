from datetime import datetime, timezone, date
from sqlalchemy.orm import Session
from app.models.canonical import Position


def insert(
    db: Session,
    account_id: str,
    drop_id: str,
    as_of_date: date,
    symbol: str,
    symbol_raw: str,
    cusip: str | None,
    description: str | None,
    asset_class: str,
    quantity: str,
    price: str,
    market_value: str,
    currency: str,
) -> Position:
    now = datetime.now(timezone.utc)
    p = Position(
        account_id=account_id,
        drop_id=drop_id,
        as_of_date=as_of_date,
        symbol=symbol,
        symbol_raw=symbol_raw,
        cusip=cusip or None,
        description=description,
        asset_class=asset_class,
        quantity=quantity,
        price=price,
        market_value=market_value,
        currency=currency,
        ingested_at=now,
    )
    db.add(p)
    return p


def get_for_drop(db: Session, drop_id: str) -> list[Position]:
    return db.query(Position).filter_by(drop_id=drop_id).all()


def get_for_account_and_drop(db: Session, account_id: str, drop_id: str) -> list[Position]:
    return (
        db.query(Position)
        .filter_by(account_id=account_id, drop_id=drop_id)
        .all()
    )
