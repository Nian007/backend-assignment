from datetime import datetime, timezone, date
from sqlalchemy.orm import Session
from app.models.canonical import Account, Ownership


def upsert(
    db: Session,
    account_number: str,
    registration_type: str | None,
    status: str,
    base_currency: str,
    open_date: date | None,
    source_id: str,
) -> tuple[Account, bool]:
    """Upsert by canonical account_number. Returns (account, was_created)."""
    now = datetime.now(timezone.utc)
    existing = db.query(Account).filter_by(account_number=account_number).first()
    if existing:
        existing.registration_type = registration_type
        existing.status = status
        existing.base_currency = base_currency
        existing.open_date = open_date
        existing.source_id = source_id
        existing.updated_at = now
        return existing, False
    a = Account(
        account_number=account_number,
        registration_type=registration_type,
        status=status,
        base_currency=base_currency,
        open_date=open_date,
        source_id=source_id,
        ingested_at=now,
        updated_at=now,
    )
    db.add(a)
    return a, True


def get_by_account_number(db: Session, account_number: str) -> Account | None:
    return db.query(Account).filter_by(account_number=account_number).first()


def get_by_household(db: Session, household_id: str) -> list[Account]:
    return db.query(Account).filter_by(household_id=household_id).all()


def get_unlinked(db: Session) -> list[Account]:
    return db.query(Account).filter(Account.household_id.is_(None)).all()


def link_accounts_to_households(db: Session) -> int:
    """
    Walk every person's crm_account_refs and assign account.household_id + ownership.
    Returns the count of newly linked accounts.
    """
    from app.models.canonical import Person
    linked = 0
    persons = db.query(Person).filter(Person.crm_account_refs.isnot(None)).all()
    for person in persons:
        if not person.household_id or not person.crm_account_refs:
            continue
        for ref in person.crm_account_refs:
            account = db.query(Account).filter_by(account_number=ref).first()
            if not account:
                continue
            if account.household_id is None:
                account.household_id = person.household_id
                linked += 1
            upsert_ownership(db, person.id, account.id,
                             _derive_role(account.registration_type, person.role))
    return linked


def upsert_ownership(db: Session, person_id: str, account_id: str, role: str) -> Ownership:
    existing = db.query(Ownership).filter_by(
        person_id=person_id, account_id=account_id
    ).first()
    if existing:
        existing.role = role
        return existing
    o = Ownership(person_id=person_id, account_id=account_id, role=role)
    db.add(o)
    return o


def _derive_role(registration_type: str | None, person_role: str | None) -> str:
    if person_role and person_role.lower() == "trustee":
        return "trustee"
    if registration_type == "JOINT":
        return "joint_owner"
    return "owner"
