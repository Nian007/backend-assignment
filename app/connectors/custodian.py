"""
Meridian Custody file connector.

Processes CSV drop folders. Each drop is ingested atomically in one DB transaction.
Effective snapshot per as_of_date is determined at query time via the supersedes chain
(not by delivery order or folder name).

Idempotency:
  - Same drop_id + same file hashes → no-op.
  - Same drop_id + different file hashes → CONFLICT, reject.
  - New drop_id → validate manifest, then ingest.

account.primary_tax_id from CSV is read and immediately discarded — never stored.
"""
import csv
import hashlib
import json
import io
from datetime import datetime, timezone, date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import ClassVar

from sqlalchemy.orm import Session

from app.connectors.base import ConnectorBase, SyncResult
from app.core.logging import get_logger
from app.core.settings import get_settings
from app.repositories import accounts as account_repo
from app.repositories import custodian_drops as drop_repo
from app.repositories import positions as position_repo
from app.repositories import valuations as valuation_repo


_STRIP_CURRENCY = str.maketrans("", "", "$, ")


def _payload_hash(file_infos: list[dict]) -> str:
    """Stable hash of all file SHA-256s, sorted by filename. Detects re-delivery with changed files."""
    combined = "".join(
        info["sha256"].lower()
        for info in sorted(file_infos, key=lambda x: x["name"])
    )
    return hashlib.sha256(combined.encode()).hexdigest()

_EXPECTED_ACCOUNT_COLS = {
    "account_number", "account_title", "registration_type",
    "open_date", "status", "base_currency", "primary_tax_id",
}
_EXPECTED_POSITION_COLS = {
    "account_number", "as_of_date", "symbol", "cusip",
    "description", "asset_class", "quantity", "price", "market_value", "currency",
}
_EXPECTED_BALANCE_COLS = {
    "account_number", "as_of_date", "total_market_value", "cash_balance",
}


def _parse_decimal(raw: str) -> Decimal:
    """Parse a possibly-formatted currency string into exact Decimal."""
    cleaned = raw.strip().lstrip("$").replace(",", "").strip()
    return Decimal(cleaned)


def _parse_date(raw: str) -> date:
    """Parse MM/DD/YYYY or YYYY-MM-DD."""
    raw = raw.strip()
    if "/" in raw:
        return datetime.strptime(raw, "%m/%d/%Y").date()
    return datetime.strptime(raw, "%Y-%m-%d").date()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65_536), b""):
            h.update(chunk)
    return h.hexdigest()


def _count_data_rows(path: Path) -> int:
    """Count CSV data rows (excludes header)."""
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        rows = list(reader)
    return max(len(rows) - 1, 0)


class CustodianConnector:
    source_name: ClassVar[str] = "meridian_custody"

    def __init__(self) -> None:
        self._settings = get_settings()
        self._log = get_logger(self.source_name)

    def run(self, run_id: str, db: Session, checkpoint: str | None) -> SyncResult:
        result = SyncResult(source=self.source_name, run_id=run_id)
        log = self._log.bind(run_id=run_id)

        drops_root = Path(self._settings.custodian_drops_dir)
        if not drops_root.exists():
            result.errors.append(f"custodian_drops_dir not found: {drops_root}")
            log.error("drops_dir_missing", path=str(drops_root))
            return result

        manifest_paths = sorted(drops_root.glob("*/manifest.json"))
        log.info("discovered_manifests", count=len(manifest_paths))

        for manifest_path in manifest_paths:
            drop_dir = manifest_path.parent
            try:
                ingested, skipped = self._process_drop(drop_dir, db, log)
                result.records_ingested += ingested
                result.records_skipped += skipped
            except _DropConflictError as exc:
                result.errors.append(str(exc))
                log.error("drop_conflict", drop_dir=drop_dir.name, error=str(exc))
            except _DropValidationError as exc:
                result.errors.append(str(exc))
                log.error("drop_validation_failed", drop_dir=drop_dir.name, error=str(exc))
            except Exception as exc:  # noqa: BLE001
                result.errors.append(f"{drop_dir.name}: unexpected {type(exc).__name__}")
                log.error("drop_unexpected_error",
                          drop_dir=drop_dir.name, error_type=type(exc).__name__)

        # After all drops, link accounts to households via CRM refs
        linked = account_repo.link_accounts_to_households(db)
        db.commit()
        log.info("accounts_linked_to_households", count=linked)

        result.new_checkpoint = date.today().isoformat()
        log.info("custodian_sync_complete",
                 ingested=result.records_ingested,
                 skipped=result.records_skipped,
                 errors=len(result.errors))
        return result

    # ------------------------------------------------------------------ #
    # Per-drop processing                                                  #
    # ------------------------------------------------------------------ #

    def _process_drop(
        self,
        drop_dir: Path,
        db: Session,
        log,
    ) -> tuple[int, int]:
        manifest = self._load_manifest(drop_dir)
        drop_id = manifest["drop_id"]
        log = log.bind(drop_id=drop_id, as_of_date=manifest["as_of_date"])

        # Idempotency check
        current_phash = _payload_hash(manifest["files"])
        existing = drop_repo.get_by_drop_id(db, drop_id)
        if existing:
            if existing.status == "ingested":
                # Compare stored payload hash to detect re-delivery with different content
                if existing.payload_hash and existing.payload_hash != current_phash:
                    raise _DropConflictError(
                        f"{drop_id}: conflict — same drop_id re-delivered with different content"
                    )
                log.info("drop_already_ingested_skipping")
                return 0, 1
            # Status is 'pending' or 'failed' — attempt to re-ingest
            log.info("drop_re_ingesting", prev_status=existing.status)

        # Validate all files before touching the DB
        file_infos = {f["name"]: f for f in manifest["files"]}
        for filename, info in file_infos.items():
            file_path = drop_dir / filename
            if not file_path.exists():
                raise _DropValidationError(f"{drop_id}: missing file {filename}")
            actual_hash = _sha256_file(file_path)
            if actual_hash.lower() != info["sha256"].lower():
                raise _DropValidationError(
                    f"{drop_id}: SHA-256 mismatch for {filename}: "
                    f"expected {info['sha256']} got {actual_hash}"
                )
            actual_rows = _count_data_rows(file_path)
            if actual_rows != info["row_count"]:
                raise _DropValidationError(
                    f"{drop_id}: row count mismatch for {filename}: "
                    f"expected {info['row_count']} got {actual_rows}"
                )

        log.info("drop_validation_passed")

        # Validate supersedes relationship
        supersedes = manifest.get("supersedes")
        if supersedes:
            parent = drop_repo.get_by_drop_id(db, supersedes)
            if parent and parent.as_of_date.isoformat() != manifest["as_of_date"]:
                raise _DropValidationError(
                    f"{drop_id}: supersedes {supersedes} but as_of_date differs"
                )

        # Ingest atomically
        delivered_at = datetime.strptime(
            manifest["delivered_at"], "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=timezone.utc)
        as_of = date.fromisoformat(manifest["as_of_date"])

        if existing:
            drop = existing
            drop.status = "pending"
        else:
            drop = drop_repo.create(
                db,
                drop_id=drop_id,
                custodian=manifest["custodian"],
                as_of_date=as_of,
                delivered_at=delivered_at,
                snapshot_type=manifest.get("snapshot_type", "FULL"),
                supersedes=supersedes,
                payload_hash=current_phash,
            )
        db.flush()

        try:
            ingested = self._ingest_files(
                db, drop, as_of, drop_dir, log
            )
            drop_repo.set_status(db, drop, "ingested")
            drop.payload_hash = current_phash
            db.commit()
            log.info("drop_ingested", accounts=ingested["accounts"],
                     positions=ingested["positions"], valuations=ingested["valuations"])
            return ingested["accounts"] + ingested["positions"] + ingested["valuations"], 0
        except Exception:
            db.rollback()
            raise

    def _ingest_files(
        self,
        db: Session,
        drop,
        as_of: date,
        drop_dir: Path,
        log,
    ) -> dict[str, int]:
        counts: dict[str, int] = {"accounts": 0, "positions": 0, "valuations": 0}

        # 1. Accounts
        accounts_by_number: dict[str, object] = {}
        accounts_path = drop_dir / "accounts.csv"
        with accounts_path.open(newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                acc_num = row["account_number"].strip()
                # primary_tax_id is read then immediately discarded — never stored
                _ = row.get("primary_tax_id")

                open_date: date | None = None
                raw_od = row.get("open_date", "").strip()
                if raw_od:
                    try:
                        open_date = _parse_date(raw_od)
                    except ValueError:
                        log.warning("invalid_open_date",
                                    account_number="****" + acc_num[-4:],
                                    raw_value=raw_od)

                account, _ = account_repo.upsert(
                    db,
                    account_number=acc_num,
                    registration_type=row.get("registration_type") or None,
                    status=row.get("status", "OPEN"),
                    base_currency=row.get("base_currency", "USD"),
                    open_date=open_date,
                    source_id=acc_num,
                )
                accounts_by_number[acc_num] = account
                counts["accounts"] += 1

        # Flush so accounts.id values are populated before FK references below
        db.flush()

        # 2. Positions
        positions_path = drop_dir / "positions.csv"
        with positions_path.open(newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                acc_num = row["account_number"].strip()
                account = accounts_by_number.get(acc_num)
                if account is None:
                    account = account_repo.get_by_account_number(db, acc_num)
                if account is None:
                    log.warning("position_unknown_account",
                                account_number="****" + acc_num[-4:])
                    continue

                symbol_raw = row["symbol"].strip()
                symbol = symbol_raw.upper()

                try:
                    qty = str(_parse_decimal(row["quantity"]))
                    price = str(_parse_decimal(row["price"]))
                    mv = str(_parse_decimal(row["market_value"]))
                except (InvalidOperation, KeyError) as exc:
                    log.warning("position_decimal_parse_error",
                                account_number="****" + acc_num[-4:],
                                symbol=symbol,
                                error=str(exc))
                    continue

                position_repo.insert(
                    db,
                    account_id=account.id,
                    drop_id=drop.id,          # FK → custodian_drops.id (UUID PK)
                    as_of_date=_parse_date(row["as_of_date"]),
                    symbol=symbol,
                    symbol_raw=symbol_raw,
                    cusip=row.get("cusip", "").strip() or None,
                    description=row.get("description", "").strip() or None,
                    asset_class=row.get("asset_class", "").strip().upper() or "UNKNOWN",
                    quantity=qty,
                    price=price,
                    market_value=mv,
                    currency=row.get("currency", "USD").strip(),
                )
                counts["positions"] += 1

        # 3. Balances → Valuations
        balances_path = drop_dir / "balances.csv"
        with balances_path.open(newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                acc_num = row["account_number"].strip()
                account = accounts_by_number.get(acc_num)
                if account is None:
                    account = account_repo.get_by_account_number(db, acc_num)
                if account is None:
                    log.warning("balance_unknown_account",
                                account_number="****" + acc_num[-4:])
                    continue

                try:
                    tmv = str(_parse_decimal(row["total_market_value"]))
                    cash = str(_parse_decimal(row["cash_balance"]))
                except (InvalidOperation, KeyError) as exc:
                    log.warning("balance_decimal_parse_error",
                                account_number="****" + acc_num[-4:],
                                error=str(exc))
                    continue

                # balances.csv has no currency column; use account's base_currency
                currency = account.base_currency or "USD"

                valuation_repo.insert(
                    db,
                    account_id=account.id,
                    drop_id=drop.id,          # FK → custodian_drops.id (UUID PK)
                    as_of_date=_parse_date(row["as_of_date"]),
                    total_market_value=tmv,
                    cash_balance=cash,
                    currency=currency,
                )
                counts["valuations"] += 1

        return counts

    # ------------------------------------------------------------------ #
    # Helpers                                                              #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _load_manifest(drop_dir: Path) -> dict:
        manifest_path = drop_dir / "manifest.json"
        if not manifest_path.exists():
            raise _DropValidationError(f"No manifest.json in {drop_dir.name}")
        with manifest_path.open(encoding="utf-8") as fh:
            return json.load(fh)



class _DropValidationError(Exception):
    """Raised when a drop fails manifest/hash/row-count validation."""


class _DropConflictError(Exception):
    """Raised when a previously-ingested drop is re-delivered with different content."""
