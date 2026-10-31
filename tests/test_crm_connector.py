"""
CRM connector tests.

Covers:
- Cursor pagination (single page + multi-page)
- Inclusive timestamp overlap / idempotent upsert
- Retry on 429 (Retry-After honoured)
- Retry on 503 (exponential backoff)
- Failure mid-pagination does NOT advance checkpoint
- Soft deletion (deleted_at propagated, tax_id still hashed)
- Replaying a sync is idempotent (upsert counts)
"""
import time
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

import pytest
import httpx
from pytest_httpx import HTTPXMock
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.connectors.crm import CRMConnector
from app.models.canonical import Household, Person


# ------------------------------------------------------------------ #
# Fixtures                                                             #
# ------------------------------------------------------------------ #

@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    yield session
    session.close()
    Base.metadata.drop_all(engine)


@pytest.fixture()
def policy():
    from cryptography.fernet import Fernet
    from app.policy.engine import PolicyEngine
    key = Fernet.generate_key().decode()
    return PolicyEngine(
        policy_path="contracts/data_policy.yaml",
        data_encryption_key=key,
        hmac_secret_key="deadbeefdeadbeefdeadbeefdeadbeef",
    )


@pytest.fixture()
def connector(policy):
    with patch("app.connectors.crm.get_settings") as mock_settings:
        settings = MagicMock()
        settings.crm_base_url = "http://mock-crm"
        settings.crm_token = "test-token"
        settings.crm_page_limit = 25
        settings.crm_max_retries = 3
        settings.crm_retry_base_delay = 0.01
        mock_settings.return_value = settings
        yield CRMConnector(policy=policy)


def _hh_page(records, next_cursor=None):
    return {"data": records, "meta": {"next_cursor": next_cursor}}


def _hh_record(hh_id="H-1001", name="Test HH", ts="2026-10-01T10:00:00Z", deleted_at=None):
    return {
        "id": hh_id, "name": name, "primary_advisor": "Adv1",
        "risk_profile": "moderate", "service_tier": "premium",
        "updated_at": ts, "deleted_at": deleted_at,
        "contacts": [],
    }


def _ct_record(ct_id="C-2001", hh_id="H-1001", ts="2026-10-01T10:00:00Z",
               deleted_at=None, refs=None, tax_id="123-45-6789"):
    return {
        "id": ct_id, "household_id": hh_id, "first_name": "Alice", "last_name": "Smith",
        "role": "Primary", "tax_id": tax_id, "email": "alice@example.com",
        "phone": "555-1234", "date_of_birth": "1980-01-15", "notes": "Good client",
        "custodian_account_numbers": refs or [],
        "updated_at": ts, "deleted_at": deleted_at,
    }


# ------------------------------------------------------------------ #
# Single-page sync                                                     #
# ------------------------------------------------------------------ #

def test_single_page_sync(connector, db_session, httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url="http://mock-crm/v2/households?limit=25&include_deleted=true",
        json=_hh_page([_hh_record("H-1001")]),
    )
    httpx_mock.add_response(
        url="http://mock-crm/v2/contacts?limit=25&include_deleted=true",
        json=_hh_page([_ct_record("C-2001", "H-1001")]),
    )

    result = connector.run("run_1", db_session, checkpoint=None)

    assert result.records_ingested == 2
    assert result.records_skipped == 0
    assert len(result.errors) == 0
    assert db_session.query(Household).count() == 1
    assert db_session.query(Person).count() == 1


# ------------------------------------------------------------------ #
# Multi-page cursor pagination                                         #
# ------------------------------------------------------------------ #

def test_multi_page_pagination(connector, db_session, httpx_mock: HTTPXMock):
    # Page 1 → cursor="page2"
    httpx_mock.add_response(
        url="http://mock-crm/v2/households?limit=25&include_deleted=true",
        json=_hh_page([_hh_record("H-1001")], next_cursor="page2"),
    )
    # Page 2 → no cursor
    httpx_mock.add_response(
        url="http://mock-crm/v2/households?limit=25&include_deleted=true&cursor=page2",
        json=_hh_page([_hh_record("H-1002", "HH Two", ts="2026-10-01T10:00:01Z")]),
    )
    httpx_mock.add_response(
        url="http://mock-crm/v2/contacts?limit=25&include_deleted=true",
        json=_hh_page([]),
    )

    result = connector.run("run_1", db_session, checkpoint=None)

    assert result.records_ingested == 2
    assert db_session.query(Household).count() == 2


# ------------------------------------------------------------------ #
# Incremental sync — inclusive timestamp overlap is idempotent        #
# ------------------------------------------------------------------ #

def test_incremental_idempotent_replay(connector, db_session, httpx_mock: HTTPXMock):
    """
    Records at the checkpoint boundary are fetched again (updated_since is inclusive).
    Because upserts are idempotent the second run must not create duplicate records.
    """
    checkpoint = "2026-10-01T10:00:00Z"

    # Both runs return the same record at the boundary timestamp
    for _ in range(2):
        httpx_mock.add_response(
            url=f"http://mock-crm/v2/households?limit=25&include_deleted=true&updated_since={checkpoint}",
            json=_hh_page([_hh_record("H-1001", ts=checkpoint)]),
        )
        httpx_mock.add_response(
            url=f"http://mock-crm/v2/contacts?limit=25&include_deleted=true&updated_since={checkpoint}",
            json=_hh_page([]),
        )

    # First sync — should create
    r1 = connector.run("run_1", db_session, checkpoint=checkpoint)
    assert db_session.query(Household).count() == 1

    # Second sync at same checkpoint — same record, should upsert (skip)
    r2 = connector.run("run_2", db_session, checkpoint=checkpoint)
    assert db_session.query(Household).count() == 1  # no duplicate
    assert r2.records_skipped >= 1


# ------------------------------------------------------------------ #
# Retry on 429 (respects Retry-After)                                 #
# ------------------------------------------------------------------ #

def test_retry_429_respects_retry_after(connector, db_session, httpx_mock: HTTPXMock):
    # First request → 429
    httpx_mock.add_response(
        url="http://mock-crm/v2/households?limit=25&include_deleted=true",
        status_code=429,
        headers={"Retry-After": "0.01"},
    )
    # Second request → success
    httpx_mock.add_response(
        url="http://mock-crm/v2/households?limit=25&include_deleted=true",
        json=_hh_page([_hh_record("H-1001")]),
    )
    httpx_mock.add_response(
        url="http://mock-crm/v2/contacts?limit=25&include_deleted=true",
        json=_hh_page([]),
    )

    with patch("time.sleep") as mock_sleep:
        result = connector.run("run_1", db_session, checkpoint=None)

    assert len(result.errors) == 0
    assert result.records_ingested == 1
    # sleep was called with the Retry-After value
    mock_sleep.assert_called_once_with(0.01)


# ------------------------------------------------------------------ #
# Retry on 503                                                         #
# ------------------------------------------------------------------ #

def test_retry_503_succeeds_on_second_attempt(connector, db_session, httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url="http://mock-crm/v2/households?limit=25&include_deleted=true",
        status_code=503,
    )
    httpx_mock.add_response(
        url="http://mock-crm/v2/households?limit=25&include_deleted=true",
        json=_hh_page([_hh_record("H-1001")]),
    )
    httpx_mock.add_response(
        url="http://mock-crm/v2/contacts?limit=25&include_deleted=true",
        json=_hh_page([]),
    )

    with patch("time.sleep"):
        result = connector.run("run_1", db_session, checkpoint=None)

    assert len(result.errors) == 0
    assert result.records_ingested == 1


# ------------------------------------------------------------------ #
# Checkpoint NOT advanced on mid-pagination failure                   #
# ------------------------------------------------------------------ #

def test_checkpoint_not_advanced_on_page_failure(connector, db_session, httpx_mock: HTTPXMock):
    """
    Page 1 succeeds, page 2 returns persistent 503 (exhausts retries).
    The SyncResult must have no new_checkpoint.
    """
    httpx_mock.add_response(
        url="http://mock-crm/v2/households?limit=25&include_deleted=true",
        json=_hh_page([_hh_record("H-1001")], next_cursor="page2"),
    )
    for _ in range(3):  # all retry attempts fail
        httpx_mock.add_response(
            url="http://mock-crm/v2/households?limit=25&include_deleted=true&cursor=page2",
            status_code=503,
        )

    with patch("time.sleep"):
        result = connector.run("run_1", db_session, checkpoint=None)

    # No checkpoint because sync did not fully complete
    assert len(result.errors) > 0
    assert result.new_checkpoint is None


# ------------------------------------------------------------------ #
# Soft delete is propagated                                            #
# ------------------------------------------------------------------ #

def test_soft_delete_propagated(connector, db_session, httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url="http://mock-crm/v2/households?limit=25&include_deleted=true",
        json=_hh_page([_hh_record("H-1001", ts="2026-10-05T08:00:00Z",
                                   deleted_at="2026-10-05T08:00:00Z")]),
    )
    httpx_mock.add_response(
        url="http://mock-crm/v2/contacts?limit=25&include_deleted=true",
        json=_hh_page([]),
    )

    connector.run("run_1", db_session, checkpoint=None)

    hh = db_session.query(Household).filter_by(source_id="H-1001").one()
    assert hh.deleted_at is not None
    assert hh.status == "deleted"


# ------------------------------------------------------------------ #
# Tax ID is hashed — never stored raw                                 #
# ------------------------------------------------------------------ #

def test_tax_id_never_stored_raw(connector, db_session, httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url="http://mock-crm/v2/households?limit=25&include_deleted=true",
        json=_hh_page([_hh_record("H-1001")]),
    )
    httpx_mock.add_response(
        url="http://mock-crm/v2/contacts?limit=25&include_deleted=true",
        json=_hh_page([_ct_record("C-2001", tax_id="123-45-6789")]),
    )

    connector.run("run_1", db_session, checkpoint=None)

    person = db_session.query(Person).filter_by(source_id="C-2001").one()
    assert person.tax_id_hash is not None
    assert "123" not in person.tax_id_hash
    assert "45" not in person.tax_id_hash
    assert "6789" not in person.tax_id_hash
    # Hash should look like a hex string
    assert len(person.tax_id_hash) == 64


# ------------------------------------------------------------------ #
# Account number normalization                                         #
# ------------------------------------------------------------------ #

def test_account_number_normalization(connector, db_session, httpx_mock: HTTPXMock):
    """Various CRM formats normalise to NNNN-NNNN."""
    httpx_mock.add_response(
        url="http://mock-crm/v2/households?limit=25&include_deleted=true",
        json=_hh_page([_hh_record("H-1001")]),
    )
    httpx_mock.add_response(
        url="http://mock-crm/v2/contacts?limit=25&include_deleted=true",
        json=_hh_page([_ct_record(
            "C-2001", refs=["88124401", "8812 4450", "8812-4402", "invalid"]
        )]),
    )

    connector.run("run_1", db_session, checkpoint=None)

    person = db_session.query(Person).filter_by(source_id="C-2001").one()
    assert "8812-4401" in person.crm_account_refs
    assert "8812-4450" in person.crm_account_refs
    assert "8812-4402" in person.crm_account_refs
    assert "invalid" not in person.crm_account_refs
    assert len(person.crm_account_refs) == 3
