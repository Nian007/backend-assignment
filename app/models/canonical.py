import uuid
from datetime import datetime, date

from sqlalchemy import (
    Column, String, Text, DateTime, Date, Integer,
    ForeignKey, UniqueConstraint, JSON,
)
from sqlalchemy.orm import relationship

from app.core.database import Base


def _new_id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex}"


class Household(Base):
    __tablename__ = "households"

    id = Column(String, primary_key=True, default=lambda: _new_id("hh_"))
    source_id = Column(String, nullable=False, unique=True, index=True)  # CRM e.g. H-1001
    name = Column(String, nullable=False)
    primary_advisor = Column(String)
    risk_profile = Column(String)
    service_tier = Column(String)
    status = Column(String, nullable=False, default="active")  # active | deleted
    ingested_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=False)
    deleted_at = Column(DateTime, nullable=True)

    persons = relationship("Person", back_populates="household")
    accounts = relationship("Account", back_populates="household")


class Person(Base):
    __tablename__ = "persons"

    id = Column(String, primary_key=True, default=lambda: _new_id("per_"))
    household_id = Column(String, ForeignKey("households.id"), nullable=True, index=True)
    source_id = Column(String, nullable=False, unique=True, index=True)  # CRM e.g. C-2001
    first_name = Column(String, nullable=False)
    last_name = Column(String, nullable=False)
    role = Column(String)
    # PII — stored per data policy
    tax_id_hash = Column(String, nullable=True)   # keyed HMAC-SHA256; never the raw value
    email_enc = Column(Text, nullable=True)        # Fernet-encrypted
    phone_enc = Column(Text, nullable=True)        # Fernet-encrypted
    dob_enc = Column(Text, nullable=True)          # Fernet-encrypted
    notes_enc = Column(Text, nullable=True)        # Fernet-encrypted; redact_free_text applied first
    crm_account_refs = Column(JSON, nullable=True) # normalized NNNN-NNNN refs from CRM custodian_account_numbers
    ingested_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=False)
    deleted_at = Column(DateTime, nullable=True)

    household = relationship("Household", back_populates="persons")
    ownerships = relationship("Ownership", back_populates="person")


class Account(Base):
    __tablename__ = "accounts"

    id = Column(String, primary_key=True, default=lambda: _new_id("acc_"))
    household_id = Column(String, ForeignKey("households.id"), nullable=True, index=True)
    account_number = Column(String, nullable=False, unique=True, index=True)  # canonical NNNN-NNNN
    registration_type = Column(String)
    status = Column(String, nullable=False, default="OPEN")
    base_currency = Column(String, nullable=False, default="USD")
    open_date = Column(Date, nullable=True)
    source_id = Column(String, nullable=True)  # custodian account_number as delivered
    ingested_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=False)
    # NOTE: account.primary_tax_id from custodian CSVs is NEVER stored here.

    household = relationship("Household", back_populates="accounts")
    ownerships = relationship("Ownership", back_populates="account")
    positions = relationship("Position", back_populates="account")
    valuations = relationship("Valuation", back_populates="account")


class Ownership(Base):
    __tablename__ = "ownerships"
    __table_args__ = (UniqueConstraint("person_id", "account_id", name="uq_person_account"),)

    id = Column(String, primary_key=True, default=lambda: _new_id("own_"))
    person_id = Column(String, ForeignKey("persons.id"), nullable=False)
    account_id = Column(String, ForeignKey("accounts.id"), nullable=False)
    role = Column(String, nullable=False, default="owner")

    person = relationship("Person", back_populates="ownerships")
    account = relationship("Account", back_populates="ownerships")


class CustodianDrop(Base):
    __tablename__ = "custodian_drops"

    id = Column(String, primary_key=True, default=lambda: _new_id("drop_"))
    drop_id = Column(String, nullable=False, unique=True, index=True)  # e.g. MC-20261008-01
    custodian = Column(String, nullable=False)
    as_of_date = Column(Date, nullable=False)
    delivered_at = Column(DateTime, nullable=False)
    snapshot_type = Column(String, nullable=False, default="FULL")
    supersedes = Column(String, nullable=True)  # drop_id of the drop this corrects
    status = Column(String, nullable=False, default="pending")
    payload_hash = Column(String, nullable=True)  # combined hash of manifest file SHA-256s
    ingested_at = Column(DateTime, nullable=False)

    positions = relationship("Position", back_populates="drop")
    valuations = relationship("Valuation", back_populates="drop")


class Position(Base):
    __tablename__ = "positions"

    id = Column(String, primary_key=True, default=lambda: _new_id("pos_"))
    account_id = Column(String, ForeignKey("accounts.id"), nullable=False, index=True)
    drop_id = Column(String, ForeignKey("custodian_drops.id"), nullable=False, index=True)
    as_of_date = Column(Date, nullable=False)
    symbol = Column(String, nullable=False)       # uppercased for matching
    symbol_raw = Column(String, nullable=False)   # original value as delivered
    cusip = Column(String, nullable=True)
    description = Column(String)
    asset_class = Column(String, nullable=False)
    quantity = Column(String, nullable=False)      # exact Decimal string
    price = Column(String, nullable=False)         # exact Decimal string
    market_value = Column(String, nullable=False)  # exact Decimal string
    currency = Column(String, nullable=False, default="USD")
    ingested_at = Column(DateTime, nullable=False)

    account = relationship("Account", back_populates="positions")
    drop = relationship("CustodianDrop", back_populates="positions")


class Valuation(Base):
    __tablename__ = "valuations"

    id = Column(String, primary_key=True, default=lambda: _new_id("val_"))
    account_id = Column(String, ForeignKey("accounts.id"), nullable=False, index=True)
    drop_id = Column(String, ForeignKey("custodian_drops.id"), nullable=False, index=True)
    as_of_date = Column(Date, nullable=False)
    total_market_value = Column(String, nullable=False)  # exact Decimal string
    cash_balance = Column(String, nullable=False)         # exact Decimal string
    currency = Column(String, nullable=False, default="USD")
    ingested_at = Column(DateTime, nullable=False)

    account = relationship("Account", back_populates="valuations")
    drop = relationship("CustodianDrop", back_populates="valuations")


class SyncRun(Base):
    __tablename__ = "sync_runs"

    id = Column(String, primary_key=True, default=lambda: f"run_{uuid.uuid4().hex}")
    source = Column(String, nullable=False, index=True)
    run_type = Column(String, nullable=False)  # full | incremental
    status = Column(String, nullable=False, default="running")
    started_at = Column(DateTime, nullable=False)
    completed_at = Column(DateTime, nullable=True)
    records_ingested = Column(Integer, default=0)
    records_skipped = Column(Integer, default=0)
    errors_count = Column(Integer, default=0)
    checkpoint_at = Column(String, nullable=True)   # new checkpoint value written on success
    error_detail = Column(JSON, nullable=True)      # first N error messages

    findings = relationship("Finding", back_populates="sync_run")


class SyncCheckpoint(Base):
    __tablename__ = "sync_checkpoints"

    id = Column(String, primary_key=True, default=lambda: _new_id("chk_"))
    source = Column(String, nullable=False, unique=True)
    last_successful_at = Column(String, nullable=True)  # ISO-8601; meaning is source-specific
    updated_at = Column(DateTime, nullable=False)


class Finding(Base):
    __tablename__ = "findings"

    id = Column(String, primary_key=True, default=lambda: f"fnd_{uuid.uuid4().hex}")
    code = Column(String, nullable=False, index=True)
    severity = Column(String, nullable=False)  # low | medium | high | critical
    message = Column(String, nullable=False)
    entity_type = Column(String, nullable=True)
    entity_id = Column(String, nullable=True, index=True)
    source = Column(String, nullable=True)
    drop_id = Column(String, nullable=True)
    sync_run_id = Column(String, ForeignKey("sync_runs.id"), nullable=True)
    expected_value = Column(Text, nullable=True)
    observed_value = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False)

    sync_run = relationship("SyncRun", back_populates="findings")
