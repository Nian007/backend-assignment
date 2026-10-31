"""
Reconciliation engine tests.

Covers:
- Possible duplicate person detection (without auto-merge)
- Missing CRM household finding
- Unlinked custodian account finding
- Deleted household with open account
- Duplicate position detection
- Position/balance mismatch (HIGH severity)
- Cash handling without double-counting
- Account lifecycle: closed account does not cause balance mismatch
"""
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app.core.database import Base, reset_engine
from app.models.canonical import (
    Account, CustodianDrop, Finding, Household,
    Person, Position, Valuation,
)
from app.repositories import findings as findings_repo


# ------------------------------------------------------------------ #
# Fixtures                                                             #
# ------------------------------------------------------------------ #

@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    db_url = f"sqlite:///{tmp_path / 'test.db'}"
    monkeypatch.setenv("DATA_ENCRYPTION_KEY", _fernet_key())
    monkeypatch.setenv("HMAC_SECRET_KEY", "deadbeefdeadbeefdeadbeefdeadbeef")
    monkeypatch.setenv("AGENT_API_KEY", "agent-key-test")
    monkeypatch.setenv("OPS_API_KEY", "ops-key-test")
    monkeypatch.setenv("DATABASE_URL", db_url)

    from app.core.settings import get_settings
    get_settings.cache_clear()
    reset_engine()

    from app.core.database import init_db
    init_db(db_url)
    yield
    reset_engine()
    get_settings.cache_clear()


@pytest.fixture()
def db(isolated_db):
    from app.core.database import get_session_factory
    SM = get_session_factory()
    session = SM()
    yield session
    session.close()


@pytest.fixture()
def policy():
    from app.policy.engine import PolicyEngine
    return PolicyEngine(
        policy_path="contracts/data_policy.yaml",
        data_encryption_key=_fernet_key(),
        hmac_secret_key="deadbeefdeadbeefdeadbeefdeadbeef",
    )


@pytest.fixture()
def engine(policy):
    from app.reconciliation.engine import ReconciliationEngine
    return ReconciliationEngine(policy=policy)


def _fernet_key() -> str:
    from cryptography.fernet import Fernet
    return Fernet.generate_key().decode()


_NOW = datetime.now(timezone.utc)
_TODAY = date.today()


def _hh(db, source_id="H-1001", name="Test HH", deleted=False):
    hh = Household(
        source_id=source_id,
        name=name,
        primary_advisor="Advisor",
        risk_profile="moderate",
        service_tier="standard",
        status="deleted" if deleted else "active",
        ingested_at=_NOW,
        updated_at=_NOW,
        deleted_at=_NOW if deleted else None,
    )
    db.add(hh)
    db.flush()
    return hh


def _person(db, source_id, household_id, first, last, tax_id_hash=None):
    p = Person(
        source_id=source_id,
        household_id=household_id,
        first_name=first,
        last_name=last,
        role="Primary",
        tax_id_hash=tax_id_hash,
        ingested_at=_NOW,
        updated_at=_NOW,
        deleted_at=None,
    )
    db.add(p)
    db.flush()
    return p


def _account(db, acct_num, household_id=None, status="OPEN"):
    acc = Account(
        account_number=acct_num,
        registration_type="INDIVIDUAL",
        status=status,
        base_currency="USD",
        open_date=_TODAY,
        source_id=acct_num.replace("-", ""),
        household_id=household_id,
        ingested_at=_NOW,
        updated_at=_NOW,
    )
    db.add(acc)
    db.flush()
    return acc


def _drop(db, drop_id="MC-TEST-01", as_of=None):
    d = CustodianDrop(
        drop_id=drop_id,
        custodian="MERIDIAN",
        as_of_date=as_of or _TODAY,
        delivered_at=_NOW,
        snapshot_type="FULL",
        supersedes=None,
        status="accepted",
        payload_hash="abc123",
        ingested_at=_NOW,
    )
    db.add(d)
    db.flush()
    return d


def _position(db, account_id, drop_id, symbol, market_value,
              asset_class="EQUITY", quantity="10", price="10.00"):
    pos = Position(
        account_id=account_id,
        drop_id=drop_id,
        as_of_date=_TODAY,
        symbol=symbol,
        symbol_raw=symbol.lower(),
        cusip=None,
        description=symbol,
        asset_class=asset_class,
        quantity=quantity,
        price=price,
        market_value=str(market_value),
        currency="USD",
        ingested_at=_NOW,
    )
    db.add(pos)
    db.flush()
    return pos


def _valuation(db, account_id, drop_id, total_mv, cash=0):
    v = Valuation(
        account_id=account_id,
        drop_id=drop_id,
        as_of_date=_TODAY,
        total_market_value=str(total_mv),
        cash_balance=str(cash),
        currency="USD",
        ingested_at=_NOW,
    )
    db.add(v)
    db.flush()
    return v


# ------------------------------------------------------------------ #
# Tests: duplicate persons                                             #
# ------------------------------------------------------------------ #

class TestDuplicatePersons:
    def test_same_tax_id_hash_same_household_emits_finding(self, db, engine):
        hh = _hh(db)
        _person(db, "C-2001", hh.id, "Robert", "Chen", tax_id_hash="abc123hash")
        _person(db, "C-2002", hh.id, "Bob", "Chen", tax_id_hash="abc123hash")
        db.commit()

        engine.run(db)
        findings = db.query(Finding).filter_by(code="POSSIBLE_DUPLICATE_PERSON").all()
        assert len(findings) == 1
        f = findings[0]
        # Finding must NOT contain the hash value
        assert "abc123hash" not in f.message
        assert "C-2001" in f.message or "C-2002" in f.message

    def test_different_tax_id_no_finding(self, db, engine):
        hh = _hh(db)
        _person(db, "C-2003", hh.id, "Alice", "Jones", tax_id_hash="hash_a")
        _person(db, "C-2004", hh.id, "Bob", "Smith", tax_id_hash="hash_b")
        db.commit()

        engine.run(db)
        findings = db.query(Finding).filter_by(code="POSSIBLE_DUPLICATE_PERSON").all()
        assert len(findings) == 0

    def test_records_retained_separately(self, db, engine):
        """Both persons must remain in the DB after reconciliation (no auto-merge)."""
        hh = _hh(db)
        _person(db, "C-9001", hh.id, "Robert", "Chen", tax_id_hash="same_hash")
        _person(db, "C-9002", hh.id, "Bob", "Chen", tax_id_hash="same_hash")
        db.commit()

        engine.run(db)
        assert db.query(Person).filter_by(household_id=hh.id).count() == 2


# ------------------------------------------------------------------ #
# Tests: missing CRM household                                         #
# ------------------------------------------------------------------ #

class TestMissingCrmHousehold:
    def test_person_without_household_emits_finding(self, db, engine):
        # Person with no household (household_id=None — set by CRM connector
        # when household_id references an unknown household)
        p = Person(
            source_id="C-2010",
            household_id=None,
            first_name="Jane",
            last_name="Doe",
            role="Primary",
            ingested_at=_NOW,
            updated_at=_NOW,
            deleted_at=None,
        )
        db.add(p)
        db.commit()

        engine.run(db)
        findings = db.query(Finding).filter_by(code="MISSING_CRM_HOUSEHOLD").all()
        assert len(findings) == 1
        assert "C-2010" in findings[0].message

    def test_person_with_valid_household_no_finding(self, db, engine):
        hh = _hh(db)
        _person(db, "C-9001", hh.id, "Joe", "Smith")
        db.commit()

        engine.run(db)
        assert db.query(Finding).filter_by(code="MISSING_CRM_HOUSEHOLD").count() == 0


# ------------------------------------------------------------------ #
# Tests: unlinked accounts                                             #
# ------------------------------------------------------------------ #

class TestUnlinkedAccounts:
    def test_account_without_household_emits_finding(self, db, engine):
        _account(db, "8812-4499", household_id=None)
        db.commit()

        engine.run(db)
        findings = db.query(Finding).filter_by(code="UNLINKED_ACCOUNT").all()
        assert len(findings) == 1
        # message must mask account number to last 4
        assert "4499" in findings[0].message
        assert "8812" not in findings[0].message  # full number must not appear

    def test_linked_account_no_finding(self, db, engine):
        hh = _hh(db)
        _account(db, "8812-4401", household_id=hh.id)
        db.commit()

        engine.run(db)
        assert db.query(Finding).filter_by(code="UNLINKED_ACCOUNT").count() == 0


# ------------------------------------------------------------------ #
# Tests: deleted household lifecycle                                   #
# ------------------------------------------------------------------ #

class TestDeletedHouseholdLifecycle:
    def test_deleted_household_with_open_account_emits_finding(self, db, engine):
        hh = _hh(db, deleted=True)
        _account(db, "8812-4430", household_id=hh.id, status="OPEN")
        db.commit()

        engine.run(db)
        findings = db.query(Finding).filter_by(code="DELETED_HOUSEHOLD_OPEN_ACCOUNT").all()
        assert len(findings) == 1

    def test_deleted_household_with_closed_account_no_finding(self, db, engine):
        hh = _hh(db, source_id="H-DELETED", deleted=True)
        _account(db, "8812-4431", household_id=hh.id, status="CLOSED")
        db.commit()

        engine.run(db)
        assert db.query(Finding).filter_by(code="DELETED_HOUSEHOLD_OPEN_ACCOUNT").count() == 0


# ------------------------------------------------------------------ #
# Tests: duplicate positions                                           #
# ------------------------------------------------------------------ #

class TestDuplicatePositions:
    def test_duplicate_rows_emit_finding(self, db, engine):
        hh = _hh(db)
        acc = _account(db, "8812-4401", household_id=hh.id)
        drop = _drop(db)
        # Two identical VTI rows (as in the Oct-8 data)
        _position(db, acc.id, drop.id, "VTI", "116860.00")
        _position(db, acc.id, drop.id, "VTI", "116860.00")
        db.commit()

        engine.run(db)
        findings = db.query(Finding).filter_by(code="DUPLICATE_POSITION").all()
        assert len(findings) == 1
        assert "VTI" in findings[0].message

    def test_unique_positions_no_duplicate_finding(self, db, engine):
        hh = _hh(db)
        acc = _account(db, "8812-4402", household_id=hh.id)
        drop = _drop(db, "MC-TEST-02")
        _position(db, acc.id, drop.id, "VTI", "10000.00")
        _position(db, acc.id, drop.id, "BND", "5000.00")
        db.commit()

        engine.run(db)
        assert db.query(Finding).filter_by(code="DUPLICATE_POSITION").count() == 0


# ------------------------------------------------------------------ #
# Tests: position/balance mismatch                                     #
# ------------------------------------------------------------------ #

class TestPositionBalanceMismatch:
    def test_mismatch_beyond_tolerance_emits_high_finding(self, db, engine):
        """
        Oct-8 v1 account 4402: reported total is correct but sum of positions
        includes the 10× MSFT error, creating a large mismatch.
        """
        hh = _hh(db)
        acc = _account(db, "8812-4402", household_id=hh.id)
        drop = _drop(db)
        # MSFT market_value off by 10× (4165 instead of 41650)
        _position(db, acc.id, drop.id, "MSFT", "4165.00")
        _valuation(db, acc.id, drop.id, total_mv="41650.00", cash=0)
        db.commit()

        engine.run(db)
        findings = db.query(Finding).filter_by(code="POSITION_SUM_MISMATCH").all()
        assert len(findings) == 1
        f = findings[0]
        assert f.severity == "high"
        assert "4402" in f.message

    def test_cash_not_double_counted(self, db, engine):
        """
        Account with a CASH position: total_market_value = equity + cash.
        The CASH position market_value should NOT be added to the sum —
        only cash_balance from the valuation row should be used.
        """
        hh = _hh(db)
        acc = _account(db, "8812-4410", household_id=hh.id)
        drop = _drop(db)
        # $1000 equity + $500 cash = $1500 total
        _position(db, acc.id, drop.id, "VTI", "1000.00", asset_class="EQUITY")
        _position(db, acc.id, drop.id, "CASH", "500.00", asset_class="CASH")
        _valuation(db, acc.id, drop.id, total_mv="1500.00", cash=500)
        db.commit()

        engine.run(db)
        # No mismatch because cash_balance(500) + non-cash positions(1000) == 1500
        findings = db.query(Finding).filter_by(code="POSITION_SUM_MISMATCH").all()
        assert len(findings) == 0

    def test_no_mismatch_when_totals_agree(self, db, engine):
        hh = _hh(db)
        acc = _account(db, "8812-4420", household_id=hh.id)
        drop = _drop(db)
        _position(db, acc.id, drop.id, "BND", "36200.00")
        _valuation(db, acc.id, drop.id, total_mv="36200.00", cash=0)
        db.commit()

        engine.run(db)
        assert db.query(Finding).filter_by(code="POSITION_SUM_MISMATCH").count() == 0

    def test_mismatch_within_tolerance_no_finding(self, db, engine):
        """$0.03 rounding difference — within $0.05 tolerance."""
        hh = _hh(db)
        acc = _account(db, "8812-4450", household_id=hh.id)
        drop = _drop(db)
        _position(db, acc.id, drop.id, "QQQ", "38980.03")
        _valuation(db, acc.id, drop.id, total_mv="38980.00", cash=0)
        db.commit()

        engine.run(db)
        assert db.query(Finding).filter_by(code="POSITION_SUM_MISMATCH").count() == 0
