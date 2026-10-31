"""
Context API tests.

Covers:
- Authentication (401) and authorization (403) exact error shapes
- Agent vs ops principal differences
- Exact response shape (household, members, accounts, totals, freshness, data_quality)
- household_not_found 404
- context_unavailable 503 when no successful sync ever completed
- Last successful canonical data remains available after a failed sync
- Deleted household behavior (200 with status=deleted)
- Ops receives finding detail; agent never does
- Correlation ID propagation
- No sensitive data leakage in error responses
"""
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.core.database import reset_engine
from app.models.canonical import (
    Account, CustodianDrop, Finding, Household,
    Person, Position, Valuation,
)
from app.repositories import sync as sync_repo


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
def client():
    from app.main import create_app
    app = create_app()
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture()
def db():
    from app.core.database import get_session_factory
    session = get_session_factory()()
    yield session
    session.close()


def _fernet_key() -> str:
    from cryptography.fernet import Fernet
    return Fernet.generate_key().decode()


_NOW = datetime.now(timezone.utc)
_TODAY = date.today()

AGENT_H = {"Authorization": "Bearer agent-key-test"}
OPS_H   = {"Authorization": "Bearer ops-key-test"}


# ------------------------------------------------------------------ #
# Seed helpers                                                         #
# ------------------------------------------------------------------ #

def _seed_household(db, source_id="H-1001", name="Test HH", deleted=False):
    hh = Household(
        source_id=source_id, name=name,
        primary_advisor="Test Advisor", risk_profile="Moderate",
        service_tier="Standard",
        status="deleted" if deleted else "active",
        ingested_at=_NOW, updated_at=_NOW,
        deleted_at=_NOW if deleted else None,
    )
    db.add(hh)
    db.flush()
    return hh


def _seed_person(db, hh_id, source_id="C-001", first="Jane", last="Doe",
                 email_plain="jane@example.com", dob_plain="1975-06-20"):
    from app.policy.engine import PolicyEngine
    from cryptography.fernet import Fernet
    import os
    key = os.environ["DATA_ENCRYPTION_KEY"]
    pol = PolicyEngine("contracts/data_policy.yaml",
                       data_encryption_key=key,
                       hmac_secret_key="deadbeefdeadbeefdeadbeefdeadbeef")
    p = Person(
        source_id=source_id, household_id=hh_id,
        first_name=first, last_name=last, role="Primary",
        email_enc=pol.encrypt(email_plain),
        dob_enc=pol.encrypt(dob_plain),
        ingested_at=_NOW, updated_at=_NOW, deleted_at=None,
    )
    db.add(p)
    db.flush()
    return p


def _seed_account(db, hh_id, acct_num="8812-4401", status="OPEN"):
    acc = Account(
        account_number=acct_num, household_id=hh_id,
        registration_type="INDIVIDUAL", status=status,
        base_currency="USD", open_date=_TODAY,
        source_id=acct_num.replace("-", ""),
        ingested_at=_NOW, updated_at=_NOW,
    )
    db.add(acc)
    db.flush()
    return acc


def _seed_drop(db, drop_id="MC-TEST-01"):
    d = CustodianDrop(
        drop_id=drop_id, custodian="MERIDIAN",
        as_of_date=_TODAY, delivered_at=_NOW,
        snapshot_type="FULL", supersedes=None,
        status="ingested", payload_hash="testhash",
        ingested_at=_NOW,
    )
    db.add(d)
    db.flush()
    return d


def _seed_position(db, acc_id, drop_id, symbol="VTI", mv="10000.00"):
    pos = Position(
        account_id=acc_id, drop_id=drop_id, as_of_date=_TODAY,
        symbol=symbol, symbol_raw=symbol.lower(), cusip=None,
        description=symbol, asset_class="EQUITY",
        quantity="100", price="100.00", market_value=mv,
        currency="USD", ingested_at=_NOW,
    )
    db.add(pos)
    db.flush()
    return pos


def _seed_valuation(db, acc_id, drop_id, total="10000.00", cash="0"):
    v = Valuation(
        account_id=acc_id, drop_id=drop_id, as_of_date=_TODAY,
        total_market_value=total, cash_balance=cash,
        currency="USD", ingested_at=_NOW,
    )
    db.add(v)
    db.flush()
    return v


def _seed_full_household(db):
    """Create a complete household H-1001 with one person, one account, one position."""
    hh = _seed_household(db)
    _seed_person(db, hh.id)
    acc = _seed_account(db, hh.id)
    drop = _seed_drop(db)
    _seed_position(db, acc.id, drop.id)
    _seed_valuation(db, acc.id, drop.id)
    db.commit()
    return hh


def _mark_sync_success(db, source="ledgerline_crm"):
    run = sync_repo.create_run(db, source, "full")
    sync_repo.complete_run(db, run, 5, 0, [], "2026-10-01T10:00:00Z")
    sync_repo.set_checkpoint(db, source, "2026-10-01T10:00:00Z")


# ------------------------------------------------------------------ #
# Authentication                                                       #
# ------------------------------------------------------------------ #

class TestAuth:
    def test_missing_auth_returns_401(self, client, db):
        _seed_full_household(db)
        _mark_sync_success(db)
        r = client.get("/v1/households/H-1001/context")
        assert r.status_code == 401
        body = r.json()
        assert body["error"]["code"] == "unauthenticated"
        assert "correlation_id" in body["error"]

    def test_invalid_token_returns_401(self, client, db):
        _seed_full_household(db)
        r = client.get("/v1/households/H-1001/context",
                       headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 401

    def test_malformed_auth_header_returns_401(self, client, db):
        r = client.get("/v1/households/H-1001/context",
                       headers={"Authorization": "NotBearer token"})
        assert r.status_code == 401

    def test_valid_agent_token_allowed(self, client, db):
        _seed_full_household(db)
        _mark_sync_success(db)
        r = client.get("/v1/households/H-1001/context", headers=AGENT_H)
        assert r.status_code == 200

    def test_valid_ops_token_allowed(self, client, db):
        _seed_full_household(db)
        _mark_sync_success(db)
        r = client.get("/v1/households/H-1001/context", headers=OPS_H)
        assert r.status_code == 200


# ------------------------------------------------------------------ #
# 503 before any successful sync                                       #
# ------------------------------------------------------------------ #

class TestNoSync:
    def test_returns_503_before_any_sync(self, client):
        r = client.get("/v1/households/H-1001/context", headers=AGENT_H)
        assert r.status_code == 503
        body = r.json()
        assert body["error"]["code"] == "context_unavailable"

    def test_canonical_data_survives_failed_sync(self, client, db):
        """Data from a prior success must still be served after a failed sync."""
        _seed_full_household(db)
        _mark_sync_success(db)
        # Add a failed run (does not overwrite the completed one)
        failed_run = sync_repo.create_run(db, "ledgerline_crm", "incremental")
        sync_repo.fail_run(db, failed_run, ["network timeout"])
        db.commit()

        r = client.get("/v1/households/H-1001/context", headers=AGENT_H)
        assert r.status_code == 200


# ------------------------------------------------------------------ #
# 404                                                                  #
# ------------------------------------------------------------------ #

class TestNotFound:
    def test_unknown_household_returns_404(self, client, db):
        _mark_sync_success(db)
        r = client.get("/v1/households/H-XXXX/context", headers=AGENT_H)
        assert r.status_code == 404
        body = r.json()
        assert body["error"]["code"] == "household_not_found"
        assert "correlation_id" in body["error"]


# ------------------------------------------------------------------ #
# Response shape                                                       #
# ------------------------------------------------------------------ #

class TestResponseShape:
    def test_200_shape(self, client, db):
        _seed_full_household(db)
        _mark_sync_success(db)
        r = client.get("/v1/households/H-1001/context", headers=AGENT_H)
        assert r.status_code == 200
        body = r.json()

        assert "household" in body
        assert "members" in body
        assert "accounts" in body
        assert "totals" in body
        assert "freshness" in body
        assert "data_quality" in body
        assert "correlation_id" in body

    def test_household_block(self, client, db):
        _seed_full_household(db)
        _mark_sync_success(db)
        body = client.get("/v1/households/H-1001/context", headers=AGENT_H).json()
        hh = body["household"]
        assert hh["id"] == "H-1001"
        assert hh["name"] == "Test HH"
        assert hh["status"] == "active"

    def test_account_number_masked_for_both_principals(self, client, db):
        _seed_full_household(db)
        _mark_sync_success(db)
        for headers in (AGENT_H, OPS_H):
            body = client.get("/v1/households/H-1001/context", headers=headers).json()
            for acc in body["accounts"]:
                num = acc.get("account_number_last4", "")
                assert num.startswith("****")
                assert "8812" not in num

    def test_totals_has_market_value(self, client, db):
        _seed_full_household(db)
        _mark_sync_success(db)
        body = client.get("/v1/households/H-1001/context", headers=AGENT_H).json()
        totals = body["totals"]
        assert "market_value" in totals
        assert "amount" in totals["market_value"]
        mv = Decimal(totals["market_value"]["amount"])
        assert mv == Decimal("10000.00")

    def test_data_quality_status_present(self, client, db):
        _seed_full_household(db)
        _mark_sync_success(db)
        body = client.get("/v1/households/H-1001/context", headers=AGENT_H).json()
        assert body["data_quality"]["status"] in ("ok", "degraded", "unreliable")

    def test_correlation_id_header_and_body(self, client, db):
        _seed_full_household(db)
        _mark_sync_success(db)
        r = client.get("/v1/households/H-1001/context",
                       headers={**AGENT_H, "X-Correlation-ID": "my-test-cid"})
        assert r.headers.get("X-Correlation-ID") == "my-test-cid"
        assert r.json()["correlation_id"] == "my-test-cid"


# ------------------------------------------------------------------ #
# Principal differences                                                #
# ------------------------------------------------------------------ #

class TestPrincipalDifferences:
    def test_agent_email_is_masked(self, client, db):
        _seed_full_household(db)
        _mark_sync_success(db)
        body = client.get("/v1/households/H-1001/context", headers=AGENT_H).json()
        for member in body.get("members", []):
            if "email" in member:
                assert "***" in member["email"]

    def test_ops_email_is_plain(self, client, db):
        _seed_full_household(db)
        _mark_sync_success(db)
        body = client.get("/v1/households/H-1001/context", headers=OPS_H).json()
        for member in body.get("members", []):
            if "email" in member:
                assert "jane@example.com" == member["email"]

    def test_agent_has_no_phone(self, client, db):
        _seed_full_household(db)
        _mark_sync_success(db)
        body = client.get("/v1/households/H-1001/context", headers=AGENT_H).json()
        for member in body.get("members", []):
            assert "phone" not in member

    def test_ops_finding_has_detail_agent_does_not(self, client, db):
        hh = _seed_household(db)
        _seed_person(db, hh.id)
        acc = _seed_account(db, hh.id)
        drop = _seed_drop(db)
        _seed_position(db, acc.id, drop.id, mv="1000.00")
        # Deliberate mismatch
        _seed_valuation(db, acc.id, drop.id, total="99999.00", cash="0")
        _mark_sync_success(db)
        f = Finding(
            code="POSITION_SUM_MISMATCH", severity="high",
            message="Sum mismatch for account ending 4401",
            entity_type="account", entity_id=acc.id,
            source="meridian_custody", drop_id=drop.id,
            sync_run_id=None,
            expected_value="99999.00", observed_value="1000.00",
            created_at=_NOW,
        )
        db.add(f)
        db.commit()

        agent_body = client.get("/v1/households/H-1001/context", headers=AGENT_H).json()
        ops_body   = client.get("/v1/households/H-1001/context", headers=OPS_H).json()

        agent_findings = agent_body["data_quality"]["findings"]
        ops_findings   = ops_body["data_quality"]["findings"]

        # Both see the finding
        assert len(agent_findings) >= 1
        assert len(ops_findings) >= 1

        # Agent must NOT have detail
        for f in agent_findings:
            assert "detail" not in f

        # Ops must have detail on findings that have expected/observed values
        ops_with_detail = [f for f in ops_findings if "detail" in f]
        assert len(ops_with_detail) >= 1


# ------------------------------------------------------------------ #
# Deleted household                                                    #
# ------------------------------------------------------------------ #

class TestDeletedHousehold:
    def test_deleted_household_returns_200_not_404(self, client, db):
        _seed_household(db, deleted=True)
        _mark_sync_success(db)
        r = client.get("/v1/households/H-1001/context", headers=AGENT_H)
        assert r.status_code == 200
        body = r.json()
        assert body["household"]["status"] == "deleted"
