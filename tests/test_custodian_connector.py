"""
Custodian connector tests.

Covers:
- Valid manifest + files ingest successfully
- SHA-256 mismatch rejects entire drop
- Row-count mismatch rejects entire drop
- Duplicate identical drop is a no-op
- Same drop_id + different content is a conflict error
- Correction drop supersedes original drop
- Out-of-order delivery selects the correct effective snapshot
- Decimal parsing with $ and commas
- primary_tax_id never stored
- Failed ingestion does not partially persist data
"""
import csv
import hashlib
import json
import shutil
import tempfile
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.connectors.custodian import CustodianConnector, _parse_decimal
from app.models.canonical import Account, Position, Valuation, CustodianDrop
from app.repositories import custodian_drops as drop_repo


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
    SM = sessionmaker(bind=engine)
    session = SM()
    yield session
    session.close()
    Base.metadata.drop_all(engine)


@pytest.fixture()
def drops_dir(tmp_path):
    """Returns a Path to a temp directory used as custodian_drops_dir."""
    d = tmp_path / "drops"
    d.mkdir()
    return d


@pytest.fixture()
def connector(drops_dir):
    with patch("app.connectors.custodian.get_settings") as mock_settings:
        settings = MagicMock()
        settings.custodian_drops_dir = str(drops_dir)
        mock_settings.return_value = settings
        yield CustodianConnector()


# ------------------------------------------------------------------ #
# Helpers                                                             #
# ------------------------------------------------------------------ #

def _write_csv(path: Path, rows: list[dict]) -> None:
    """Write a CSV file from a list of dicts."""
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        h.update(fh.read())
    return h.hexdigest()


def _make_drop(
    drops_dir: Path,
    drop_id: str,
    as_of_date: str = "2026-10-08",
    delivered_at: str = "2026-10-09T02:00:00Z",
    supersedes: str | None = None,
    accounts: list[dict] | None = None,
    positions: list[dict] | None = None,
    balances: list[dict] | None = None,
) -> Path:
    drop_dir = drops_dir / drop_id
    drop_dir.mkdir()

    acc_rows = accounts or [{
        "account_number": "8812-4401",
        "account_title": "TEST ACCOUNT",
        "registration_type": "INDIVIDUAL",
        "open_date": "03/14/2024",
        "status": "OPEN",
        "base_currency": "USD",
        "primary_tax_id": "123-45-6789",
    }]
    pos_rows = positions or [{
        "account_number": "8812-4401",
        "as_of_date": as_of_date,
        "symbol": "VTI",
        "cusip": "922908769",
        "description": "VANGUARD ETF",
        "asset_class": "EQUITY",
        "quantity": "100",
        "price": "292.15",
        "market_value": "29215.00",
        "currency": "USD",
    }]
    bal_rows = balances or [{
        "account_number": "8812-4401",
        "as_of_date": as_of_date,
        "total_market_value": "29215.00",
        "cash_balance": "0.00",
    }]

    acc_path = drop_dir / "accounts.csv"
    pos_path = drop_dir / "positions.csv"
    bal_path = drop_dir / "balances.csv"
    _write_csv(acc_path, acc_rows)
    _write_csv(pos_path, pos_rows)
    _write_csv(bal_path, bal_rows)

    manifest = {
        "drop_id": drop_id,
        "custodian": "Test Custodian",
        "as_of_date": as_of_date,
        "delivered_at": delivered_at,
        "snapshot_type": "FULL",
        "supersedes": supersedes,
        "files": [
            {"name": "accounts.csv", "sha256": _sha256(acc_path), "row_count": len(acc_rows)},
            {"name": "positions.csv", "sha256": _sha256(pos_path), "row_count": len(pos_rows)},
            {"name": "balances.csv", "sha256": _sha256(bal_path), "row_count": len(bal_rows)},
        ],
    }
    (drop_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return drop_dir


# ------------------------------------------------------------------ #
# Decimal parsing                                                      #
# ------------------------------------------------------------------ #

@pytest.mark.parametrize("raw,expected", [
    ("29215.00",       Decimal("29215.00")),
    ("$38,980.00",     Decimal("38980.00")),
    ("36,200.00",      Decimal("36200.00")),
    ("1",              Decimal("1")),
    ("  $1,000,000.50 ", Decimal("1000000.50")),
])
def test_parse_decimal(raw, expected):
    assert _parse_decimal(raw) == expected


# ------------------------------------------------------------------ #
# Happy path — valid drop ingest                                       #
# ------------------------------------------------------------------ #

def test_valid_drop_ingest(connector, db_session, drops_dir):
    _make_drop(drops_dir, "TEST-001")

    result = connector.run("run_1", db_session, checkpoint=None)

    assert len(result.errors) == 0
    assert db_session.query(Account).count() == 1
    assert db_session.query(Position).count() == 1
    assert db_session.query(Valuation).count() == 1
    drop = db_session.query(CustodianDrop).filter_by(drop_id="TEST-001").one()
    assert drop.status == "ingested"


# ------------------------------------------------------------------ #
# primary_tax_id never stored                                          #
# ------------------------------------------------------------------ #

def test_primary_tax_id_not_stored(connector, db_session, drops_dir):
    _make_drop(drops_dir, "TEST-001")
    connector.run("run_1", db_session, checkpoint=None)

    # The Account model has no primary_tax_id column — verify the column does not exist
    from sqlalchemy import inspect as sa_inspect
    inspector = sa_inspect(db_session.bind)
    cols = [c["name"] for c in inspector.get_columns("accounts")]
    assert "primary_tax_id" not in cols


# ------------------------------------------------------------------ #
# SHA-256 mismatch rejects drop atomically                             #
# ------------------------------------------------------------------ #

def test_sha256_mismatch_rejects_drop(connector, db_session, drops_dir):
    drop_dir = _make_drop(drops_dir, "TEST-001")
    # Tamper with positions.csv AFTER manifest was written
    pos_path = drop_dir / "positions.csv"
    content = pos_path.read_text()
    pos_path.write_text(content + "\nextra,tampered,row")

    result = connector.run("run_1", db_session, checkpoint=None)

    assert any("SHA-256" in e for e in result.errors)
    # Nothing persisted
    assert db_session.query(Account).count() == 0
    assert db_session.query(Position).count() == 0


# ------------------------------------------------------------------ #
# Row-count mismatch rejects drop                                      #
# ------------------------------------------------------------------ #

def test_row_count_mismatch_rejects_drop(connector, db_session, drops_dir):
    drop_dir = drops_dir / "TEST-001"
    drop_dir.mkdir()
    # Write accounts.csv with 2 rows
    acc_rows = [
        {"account_number": "8812-4401", "account_title": "A", "registration_type": "IND",
         "open_date": "01/01/2024", "status": "OPEN", "base_currency": "USD", "primary_tax_id": ""},
        {"account_number": "8812-4402", "account_title": "B", "registration_type": "IND",
         "open_date": "01/01/2024", "status": "OPEN", "base_currency": "USD", "primary_tax_id": ""},
    ]
    acc_path = drop_dir / "accounts.csv"
    pos_path = drop_dir / "positions.csv"
    bal_path = drop_dir / "balances.csv"
    _write_csv(acc_path, acc_rows)
    pos_rows = [{"account_number": "8812-4401", "as_of_date": "2026-10-08",
                 "symbol": "VTI", "cusip": "", "description": "", "asset_class": "EQUITY",
                 "quantity": "1", "price": "1", "market_value": "1", "currency": "USD"}]
    bal_rows = [{"account_number": "8812-4401", "as_of_date": "2026-10-08",
                 "total_market_value": "1", "cash_balance": "0"}]
    _write_csv(pos_path, pos_rows)
    _write_csv(bal_path, bal_rows)

    manifest = {
        "drop_id": "TEST-001",
        "custodian": "Test",
        "as_of_date": "2026-10-08",
        "delivered_at": "2026-10-09T00:00:00Z",
        "snapshot_type": "FULL",
        "supersedes": None,
        "files": [
            # Claim 1 row but file has 2
            {"name": "accounts.csv", "sha256": _sha256(acc_path), "row_count": 1},
            {"name": "positions.csv", "sha256": _sha256(pos_path), "row_count": 1},
            {"name": "balances.csv", "sha256": _sha256(bal_path), "row_count": 1},
        ],
    }
    (drop_dir / "manifest.json").write_text(json.dumps(manifest))

    result = connector.run("run_1", db_session, checkpoint=None)

    assert any("row count" in e.lower() for e in result.errors)
    assert db_session.query(Account).count() == 0


# ------------------------------------------------------------------ #
# Duplicate identical drop is a no-op                                  #
# ------------------------------------------------------------------ #

def test_duplicate_identical_drop_noop(connector, db_session, drops_dir):
    _make_drop(drops_dir, "TEST-001")

    r1 = connector.run("run_1", db_session, checkpoint=None)
    initial_accounts = db_session.query(Account).count()
    initial_positions = db_session.query(Position).count()

    r2 = connector.run("run_2", db_session, checkpoint=None)

    assert r2.records_skipped >= 1
    assert db_session.query(Account).count() == initial_accounts
    assert db_session.query(Position).count() == initial_positions
    assert len(r2.errors) == 0


# ------------------------------------------------------------------ #
# Same drop_id + different content is a conflict                       #
# ------------------------------------------------------------------ #

def test_drop_conflict_rejected(connector, db_session, drops_dir):
    drop_dir = _make_drop(drops_dir, "TEST-001")
    connector.run("run_1", db_session, checkpoint=None)

    # Overwrite positions.csv with different content but keep same manifest
    pos_path = drop_dir / "positions.csv"
    manifest_path = drop_dir / "manifest.json"
    old_manifest = json.loads(manifest_path.read_text())

    new_pos = [{"account_number": "8812-4401", "as_of_date": "2026-10-08",
                "symbol": "AAPL", "cusip": "", "description": "", "asset_class": "EQUITY",
                "quantity": "200", "price": "150", "market_value": "30000", "currency": "USD"}]
    _write_csv(pos_path, new_pos)
    # Update manifest with new hash but keep same drop_id
    old_manifest["files"][1]["sha256"] = _sha256(pos_path)
    old_manifest["files"][1]["row_count"] = 1
    manifest_path.write_text(json.dumps(old_manifest))

    r2 = connector.run("run_2", db_session, checkpoint=None)
    assert any("conflict" in e.lower() or "different content" in e.lower() for e in r2.errors)


# ------------------------------------------------------------------ #
# Correction drop supersedes original                                  #
# ------------------------------------------------------------------ #

def test_correction_supersedes_original(connector, db_session, drops_dir):
    # v1: market_value is wrong (off by 10x)
    v1_pos = [{
        "account_number": "8812-4401", "as_of_date": "2026-10-08",
        "symbol": "MSFT", "cusip": "594918104", "description": "MSFT", "asset_class": "EQUITY",
        "quantity": "100", "price": "416.50", "market_value": "4165.00", "currency": "USD",
    }]
    v1_bal = [{"account_number": "8812-4401", "as_of_date": "2026-10-08",
               "total_market_value": "4165.00", "cash_balance": "0.00"}]
    _make_drop(drops_dir, "DROP-v1", as_of_date="2026-10-08",
               delivered_at="2026-10-09T02:00:00Z",
               positions=v1_pos, balances=v1_bal)

    # v2 (correction): correct market_value
    v2_pos = [{
        "account_number": "8812-4401", "as_of_date": "2026-10-08",
        "symbol": "MSFT", "cusip": "594918104", "description": "MSFT", "asset_class": "EQUITY",
        "quantity": "100", "price": "416.50", "market_value": "41650.00", "currency": "USD",
    }]
    v2_bal = [{"account_number": "8812-4401", "as_of_date": "2026-10-08",
               "total_market_value": "41650.00", "cash_balance": "0.00"}]
    _make_drop(drops_dir, "DROP-v2", as_of_date="2026-10-08",
               delivered_at="2026-10-10T15:00:00Z",
               supersedes="DROP-v1",
               positions=v2_pos, balances=v2_bal)

    connector.run("run_1", db_session, checkpoint=None)

    # Effective snapshot for 2026-10-08 must be DROP-v2 (not superseded)
    effective = drop_repo.get_effective_drops(db_session)
    eff_date = date(2026, 10, 8)
    assert eff_date in effective
    eff_drop = effective[eff_date]
    assert eff_drop.drop_id == "DROP-v2"

    # Valuation from effective drop should reflect corrected value
    v2_db = db_session.query(CustodianDrop).filter_by(drop_id="DROP-v2").one()
    valuation = db_session.query(Valuation).filter_by(drop_id=v2_db.id).first()
    assert Decimal(valuation.total_market_value) == Decimal("41650.00")


# ------------------------------------------------------------------ #
# Out-of-order delivery — effective snapshot is NOT latest delivered  #
# ------------------------------------------------------------------ #

def test_out_of_order_delivery(connector, db_session, drops_dir):
    """
    Oct-9 drop arrives before the Oct-8 correction.
    Effective snapshot for Oct-8 must be the correction (DROP-v2), not DROP-v1.
    """
    _make_drop(drops_dir, "DROP-oct8-v1", as_of_date="2026-10-08",
               delivered_at="2026-10-09T02:00:00Z")
    _make_drop(drops_dir, "DROP-oct9",    as_of_date="2026-10-09",
               delivered_at="2026-10-10T02:00:00Z")

    v2_pos = [{"account_number": "8812-4401", "as_of_date": "2026-10-08",
               "symbol": "VTI", "cusip": "", "description": "", "asset_class": "EQUITY",
               "quantity": "100", "price": "292.15", "market_value": "29215.00", "currency": "USD"}]
    v2_bal = [{"account_number": "8812-4401", "as_of_date": "2026-10-08",
               "total_market_value": "29215.00", "cash_balance": "0.00"}]
    _make_drop(drops_dir, "DROP-oct8-v2", as_of_date="2026-10-08",
               delivered_at="2026-10-10T15:00:00Z",
               supersedes="DROP-oct8-v1",
               positions=v2_pos, balances=v2_bal)

    connector.run("run_1", db_session, checkpoint=None)

    effective = drop_repo.get_effective_drops(db_session)
    oct8 = date(2026, 10, 8)
    oct9 = date(2026, 10, 9)
    assert effective[oct8].drop_id == "DROP-oct8-v2"
    assert effective[oct9].drop_id == "DROP-oct9"


# ------------------------------------------------------------------ #
# Failed ingestion does not partially persist                          #
# ------------------------------------------------------------------ #

def test_failed_ingestion_no_partial_persist(connector, db_session, drops_dir):
    """If ingestion raises mid-way, transaction is rolled back — zero rows persist."""
    _make_drop(drops_dir, "TEST-001")

    # Patch position_repo.insert to raise on the first call
    with patch("app.connectors.custodian.position_repo.insert",
               side_effect=RuntimeError("simulated disk failure")):
        result = connector.run("run_1", db_session, checkpoint=None)

    assert db_session.query(Position).count() == 0
    # Account and valuation should also be rolled back
    assert db_session.query(Valuation).count() == 0
    # The drop itself should not be in 'ingested' status
    drop = db_session.query(CustodianDrop).filter_by(drop_id="TEST-001").first()
    assert drop is None or drop.status != "ingested"


# ------------------------------------------------------------------ #
# Symbol normalization — lowercase → uppercase                        #
# ------------------------------------------------------------------ #

def test_symbol_normalized_to_uppercase(connector, db_session, drops_dir):
    pos_rows = [{
        "account_number": "8812-4401", "as_of_date": "2026-10-08",
        "symbol": "voo", "cusip": "", "description": "VANGUARD S&P 500", "asset_class": "EQUITY",
        "quantity": "50", "price": "490.00", "market_value": "24500.00", "currency": "USD",
    }]
    _make_drop(drops_dir, "TEST-001", positions=pos_rows)
    connector.run("run_1", db_session, checkpoint=None)

    pos = db_session.query(Position).first()
    assert pos.symbol == "VOO"
    assert pos.symbol_raw == "voo"
