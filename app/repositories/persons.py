from datetime import datetime, timezone
from sqlalchemy.orm import Session
from app.models.canonical import Person


def upsert(
    db: Session,
    source_id: str,
    household_id: str | None,
    first_name: str,
    last_name: str,
    role: str | None,
    tax_id_hash: str | None,
    email_enc: str | None,
    phone_enc: str | None,
    dob_enc: str | None,
    notes_enc: str | None,
    crm_account_refs: list[str],
    updated_at: datetime,
    deleted_at: datetime | None,
) -> tuple[Person, bool]:
    """Upsert by source_id. Returns (person, was_created)."""
    now = datetime.now(timezone.utc)
    existing = db.query(Person).filter_by(source_id=source_id).first()
    if existing:
        existing.household_id = household_id
        existing.first_name = first_name
        existing.last_name = last_name
        existing.role = role
        existing.tax_id_hash = tax_id_hash
        existing.email_enc = email_enc
        existing.phone_enc = phone_enc
        existing.dob_enc = dob_enc
        existing.notes_enc = notes_enc
        existing.crm_account_refs = crm_account_refs
        existing.updated_at = updated_at
        existing.deleted_at = deleted_at
        return existing, False
    p = Person(
        source_id=source_id,
        household_id=household_id,
        first_name=first_name,
        last_name=last_name,
        role=role,
        tax_id_hash=tax_id_hash,
        email_enc=email_enc,
        phone_enc=phone_enc,
        dob_enc=dob_enc,
        notes_enc=notes_enc,
        crm_account_refs=crm_account_refs,
        ingested_at=now,
        updated_at=updated_at,
        deleted_at=deleted_at,
    )
    db.add(p)
    return p, True


def get_by_source_id(db: Session, source_id: str) -> Person | None:
    return db.query(Person).filter_by(source_id=source_id).first()


def get_by_household(db: Session, household_id: str) -> list[Person]:
    return db.query(Person).filter_by(household_id=household_id).all()


def get_all_with_account_refs(db: Session) -> list[Person]:
    return db.query(Person).filter(Person.crm_account_refs.isnot(None)).all()
