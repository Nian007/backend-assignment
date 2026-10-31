"""
Sync orchestrator and ops endpoint tests.

Covers:
- Full sync creates run and advances checkpoint on success
- Incremental sync uses checkpoint and creates incremental run_type
- Failed sync does NOT advance checkpoint
- Failed sync does NOT erase previously ingested canonical data
- Connector error is captured in run, checkpoint unchanged
- list_sync_runs and get_sync_run endpoints
- 401 / 403 / correct error format
- Correlation ID propagated in response header and error body
"""
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base, reset_engine
from app.connectors.base import SyncResult
from app.models.canonical import Household, SyncCheckpoint
from app.repositories import sync as sync_repo


# ------------------------------------------------------------------ #
# DB / app fixtures                                                    #
# ------------------------------------------------------------------ #

@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Each test gets its own file-based SQLite DB (required for TestClient multi-connection tests)."""
    db_file = tmp_path / "test.db"
    db_url = f"sqlite:///{db_file}"
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
def client(isolated_db):
    from app.main import create_app
    application = create_app()
    with TestClient(application, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture()
def db(isolated_db):
    from app.core.database import get_session_factory
    SM = get_session_factory()
    session = SM()
    yield session
    session.close()


@pytest.fixture()
def policy():
    from cryptography.fernet import Fernet
    from app.policy.engine import PolicyEngine
    return PolicyEngine(
        policy_path="contracts/data_policy.yaml",
        data_encryption_key=_fernet_key(),
        hmac_secret_key="deadbeefdeadbeefdeadbeefdeadbeef",
    )


def _fernet_key() -> str:
    from cryptography.fernet import Fernet
    return Fernet.generate_key().decode()


# ------------------------------------------------------------------ #
# Helpers                                                              #
# ------------------------------------------------------------------ #

def _ok_result(run_id: str, source: str) -> SyncResult:
    return SyncResult(
        source=source, run_id=run_id,
        records_ingested=5, records_skipped=1,
        errors=[], new_checkpoint="2026-10-01T10:00:00Z",
    )


def _fail_result(run_id: str, source: str) -> SyncResult:
    return SyncResult(
        source=source, run_id=run_id,
        records_ingested=0, records_skipped=0,
        errors=["CRM 503 unavailable"], new_checkpoint=None,
    )


# ------------------------------------------------------------------ #
# SyncOrchestrator unit tests                                          #
# ------------------------------------------------------------------ #

class TestSyncOrchestrator:
    def test_full_sync_on_first_run(self, db, policy):
        from app.services.sync_orchestrator import SyncOrchestrator
        orch = SyncOrchestrator(policy=policy)

        with patch.object(orch, "_build_connector") as mock_build:
            mock_conn = MagicMock()
            mock_conn.run.side_effect = lambda run_id, db, checkpoint: (
                _ok_result(run_id, "ledgerline_crm")
            )
            mock_build.return_value = mock_conn
            run = orch.run_sync("crm", db)

        assert run.status == "success"
        assert run.run_type == "full"
        assert run.records_ingested == 5
        chk = db.query(SyncCheckpoint).filter_by(source="ledgerline_crm").first()
        assert chk is not None
        assert chk.last_successful_at == "2026-10-01T10:00:00Z"

    def test_incremental_sync_after_checkpoint(self, db, policy):
        from app.services.sync_orchestrator import SyncOrchestrator
        sync_repo.set_checkpoint(db, "ledgerline_crm", "2026-09-30T00:00:00Z")
        orch = SyncOrchestrator(policy=policy)

        with patch.object(orch, "_build_connector") as mock_build:
            mock_conn = MagicMock()
            captured = {}
            def run_side_effect(run_id, db, checkpoint):
                captured["checkpoint"] = checkpoint
                return _ok_result(run_id, "ledgerline_crm")
            mock_conn.run.side_effect = run_side_effect
            mock_build.return_value = mock_conn
            run = orch.run_sync("crm", db)

        assert run.run_type == "incremental"
        assert captured["checkpoint"] == "2026-09-30T00:00:00Z"

    def test_failed_sync_does_not_advance_checkpoint(self, db, policy):
        from app.services.sync_orchestrator import SyncOrchestrator
        sync_repo.set_checkpoint(db, "ledgerline_crm", "2026-09-01T00:00:00Z")
        orch = SyncOrchestrator(policy=policy)

        with patch.object(orch, "_build_connector") as mock_build:
            mock_conn = MagicMock()
            mock_conn.run.side_effect = lambda run_id, db, checkpoint: (
                _fail_result(run_id, "ledgerline_crm")
            )
            mock_build.return_value = mock_conn
            run = orch.run_sync("crm", db)

        assert run.status == "failed"
        assert run.errors_count == 1
        chk = db.query(SyncCheckpoint).filter_by(source="ledgerline_crm").first()
        assert chk.last_successful_at == "2026-09-01T00:00:00Z"  # unchanged

    def test_connector_exception_captured_checkpoint_unchanged(self, db, policy):
        from app.services.sync_orchestrator import SyncOrchestrator
        sync_repo.set_checkpoint(db, "ledgerline_crm", "2026-09-15T00:00:00Z")
        orch = SyncOrchestrator(policy=policy)

        with patch.object(orch, "_build_connector") as mock_build:
            mock_conn = MagicMock()
            mock_conn.run.side_effect = RuntimeError("network timeout")
            mock_build.return_value = mock_conn
            run = orch.run_sync("crm", db)

        assert run.status == "failed"
        chk = db.query(SyncCheckpoint).filter_by(source="ledgerline_crm").first()
        assert chk.last_successful_at == "2026-09-15T00:00:00Z"

    def test_failed_sync_does_not_erase_canonical_data(self, db, policy):
        """Canonical data from a previous successful sync must survive a failed sync."""
        from datetime import datetime, timezone
        from app.repositories import households as hh_repo
        from app.services.sync_orchestrator import SyncOrchestrator

        # Pre-populate a household
        hh_repo.upsert(
            db,
            source_id="H-1001",
            name="Chen Family",
            primary_advisor="Advisor1",
            risk_profile="moderate",
            service_tier="premium",
            status="active",
            updated_at=datetime.now(timezone.utc),
            deleted_at=None,
        )
        db.commit()
        assert db.query(Household).count() == 1

        sync_repo.set_checkpoint(db, "ledgerline_crm", "2026-10-01T00:00:00Z")
        orch = SyncOrchestrator(policy=policy)

        with patch.object(orch, "_build_connector") as mock_build:
            mock_conn = MagicMock()
            mock_conn.run.side_effect = lambda run_id, db, checkpoint: (
                _fail_result(run_id, "ledgerline_crm")
            )
            mock_build.return_value = mock_conn
            orch.run_sync("crm", db)

        # Household must still exist
        assert db.query(Household).count() == 1


# ------------------------------------------------------------------ #
# API endpoint tests via TestClient                                    #
# ------------------------------------------------------------------ #

class TestOpsEndpoints:
    def test_no_auth_returns_401(self, client):
        r = client.get("/v1/ops/sync-runs")
        assert r.status_code == 401
        body = r.json()
        assert body["error"]["code"] == "unauthenticated"
        assert "correlation_id" in body["error"]

    def test_agent_token_on_ops_endpoint_returns_403(self, client):
        r = client.get("/v1/ops/sync-runs",
                       headers={"Authorization": "Bearer agent-key-test"})
        assert r.status_code == 403
        body = r.json()
        assert body["error"]["code"] == "forbidden"

    def test_invalid_token_returns_401(self, client):
        r = client.get("/v1/ops/sync-runs",
                       headers={"Authorization": "Bearer totally-wrong"})
        assert r.status_code == 401

    def test_correlation_id_echoed_in_response_header(self, client):
        r = client.get("/v1/ops/sync-runs",
                       headers={
                           "Authorization": "Bearer ops-key-test",
                           "X-Correlation-ID": "test-cid-abc",
                       })
        assert r.headers.get("X-Correlation-ID") == "test-cid-abc"

    def test_correlation_id_generated_when_absent(self, client):
        r = client.get("/v1/ops/sync-runs",
                       headers={"Authorization": "Bearer ops-key-test"})
        cid = r.headers.get("X-Correlation-ID")
        assert cid is not None
        assert len(cid) == 36  # UUID4 format

    def test_correlation_id_in_error_body(self, client):
        r = client.get("/v1/ops/sync-runs",
                       headers={"X-Correlation-ID": "err-cid-xyz"})
        body = r.json()
        assert body["error"]["correlation_id"] == "err-cid-xyz"

    def test_list_sync_runs_empty(self, client):
        r = client.get("/v1/ops/sync-runs",
                       headers={"Authorization": "Bearer ops-key-test"})
        assert r.status_code == 200
        assert r.json() == []

    def test_list_sync_runs_returns_runs(self, client, db):
        sync_repo.create_run(db, "ledgerline_crm", "full")
        r = client.get("/v1/ops/sync-runs",
                       headers={"Authorization": "Bearer ops-key-test"})
        assert r.status_code == 200
        assert len(r.json()) == 1
        run = r.json()[0]
        assert run["source"] == "ledgerline_crm"
        assert run["run_type"] == "full"

    def test_get_run_by_id(self, client, db):
        run = sync_repo.create_run(db, "meridian_custody", "full")
        r = client.get(f"/v1/ops/sync-runs/{run.id}",
                       headers={"Authorization": "Bearer ops-key-test"})
        assert r.status_code == 200
        assert r.json()["run_id"] == run.id

    def test_get_run_not_found(self, client):
        r = client.get("/v1/ops/sync-runs/nonexistent-run-id",
                       headers={"Authorization": "Bearer ops-key-test"})
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "run_not_found"

    def test_trigger_sync_unknown_source(self, client):
        r = client.post("/v1/ops/sync/unknown_source",
                        headers={"Authorization": "Bearer ops-key-test"})
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "unknown_source"

    def test_trigger_sync_source_aliases(self, client):
        """Both 'crm' and 'ledgerline_crm' are accepted."""
        for alias in ("crm", "ledgerline_crm", "custodian", "meridian_custody"):
            with patch("app.api.ops.SyncOrchestrator") as _:
                # Patch the orchestrator on app.state so it returns a fake run
                from app.models.canonical import SyncRun
                fake_run = SyncRun(
                    id=f"run_test_{alias}",
                    source=alias,
                    run_type="full",
                    status="success",
                    started_at=datetime.now(timezone.utc),
                    completed_at=datetime.now(timezone.utc),
                    records_ingested=0,
                    records_skipped=0,
                    errors_count=0,
                )
                # patch at the app.state level using a real TestClient
                with patch("app.services.sync_orchestrator.SyncOrchestrator.run_sync",
                           return_value=fake_run):
                    r = client.post(f"/v1/ops/sync/{alias}",
                                    headers={"Authorization": "Bearer ops-key-test"})
                    assert r.status_code == 200, f"alias={alias}: {r.json()}"
