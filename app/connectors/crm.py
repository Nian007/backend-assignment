"""
Ledgerline CRM connector.

Incremental sync using updated_since (inclusive) + idempotent upserts.
Checkpoint advances only after all pages of all endpoints succeed.
"""
import re
from datetime import datetime, timezone
from typing import ClassVar

import httpx
from sqlalchemy.orm import Session

from app.connectors.base import ConnectorBase, SyncResult
from app.core.logging import get_logger
from app.core.retry import retry_with_backoff, RetryableError, NonRetryableError
from app.core.settings import get_settings
from app.policy.engine import PolicyEngine
from app.repositories import households as household_repo
from app.repositories import persons as person_repo


_ACCOUNT_REF_RE = re.compile(r"[^0-9]")


def _normalize_account_ref(raw: str) -> str | None:
    """Strip spaces/hyphens, require exactly 8 digits, format NNNN-NNNN."""
    digits = _ACCOUNT_REF_RE.sub("", raw.strip())
    if len(digits) != 8:
        return None
    return f"{digits[:4]}-{digits[4:]}"


def _parse_crm_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


class CRMConnector:
    source_name: ClassVar[str] = "ledgerline_crm"

    def __init__(self, policy: PolicyEngine) -> None:
        self._policy = policy
        self._settings = get_settings()
        self._log = get_logger(self.source_name)

    def run(self, run_id: str, db: Session, checkpoint: str | None) -> SyncResult:
        result = SyncResult(source=self.source_name, run_id=run_id)
        log = self._log.bind(run_id=run_id, incremental=checkpoint is not None)
        max_ts: list[str] = []  # accumulates max updated_at across both endpoints

        with httpx.Client(timeout=30.0) as client:
            try:
                hi, hs = self._sync_households(client, db, checkpoint, max_ts, log)
                ci, cs = self._sync_contacts(client, db, checkpoint, max_ts, log)
            except (RetryableError, NonRetryableError, httpx.HTTPError) as exc:
                result.errors.append(str(exc))
                log.error("sync_failed", error=str(exc))
                return result
            except Exception as exc:  # noqa: BLE001
                result.errors.append(f"unexpected: {type(exc).__name__}")
                log.error("sync_unexpected_error", error_type=type(exc).__name__)
                return result

        result.records_ingested = hi + ci
        result.records_skipped = hs + cs
        result.new_checkpoint = max_ts[0] if max_ts else checkpoint
        log.info("crm_sync_complete",
                 ingested=result.records_ingested,
                 skipped=result.records_skipped,
                 new_checkpoint=result.new_checkpoint)
        return result

    # ------------------------------------------------------------------ #
    # Households                                                           #
    # ------------------------------------------------------------------ #

    def _sync_households(
        self,
        client: httpx.Client,
        db: Session,
        checkpoint: str | None,
        max_ts: list[str],
        log,
    ) -> tuple[int, int]:
        ingested = skipped = 0
        params: dict = {
            "limit": self._settings.crm_page_limit,
            "include_deleted": "true",
        }
        if checkpoint:
            params["updated_since"] = checkpoint

        cursor: str | None = None
        while True:
            if cursor:
                params["cursor"] = cursor
            else:
                params.pop("cursor", None)

            data = self._fetch_page(client, "/v2/households", params, log)

            for record in data["data"]:
                created = self._upsert_household(db, record)
                _update_max_ts(max_ts, record["updated_at"])
                if created:
                    ingested += 1
                else:
                    skipped += 1

            cursor = data["meta"].get("next_cursor")
            if not cursor:
                break

        db.flush()
        log.info("households_synced", ingested=ingested, skipped=skipped)
        return ingested, skipped

    def _upsert_household(self, db: Session, record: dict) -> bool:
        status = "deleted" if record.get("deleted_at") else "active"
        _, created = household_repo.upsert(
            db,
            source_id=record["id"],
            name=record["name"],
            primary_advisor=record.get("primary_advisor"),
            risk_profile=record.get("risk_profile"),
            service_tier=record.get("service_tier"),
            status=status,
            updated_at=_parse_crm_dt(record["updated_at"]),
            deleted_at=_parse_crm_dt(record.get("deleted_at")),
        )
        return created

    # ------------------------------------------------------------------ #
    # Contacts                                                             #
    # ------------------------------------------------------------------ #

    def _sync_contacts(
        self,
        client: httpx.Client,
        db: Session,
        checkpoint: str | None,
        max_ts: list[str],
        log,
    ) -> tuple[int, int]:
        ingested = skipped = 0
        params: dict = {
            "limit": self._settings.crm_page_limit,
            "include_deleted": "true",
        }
        if checkpoint:
            params["updated_since"] = checkpoint

        cursor: str | None = None
        while True:
            if cursor:
                params["cursor"] = cursor
            else:
                params.pop("cursor", None)

            data = self._fetch_page(client, "/v2/contacts", params, log)

            for record in data["data"]:
                created = self._upsert_contact(db, record)
                _update_max_ts(max_ts, record["updated_at"])
                if created:
                    ingested += 1
                else:
                    skipped += 1

            cursor = data["meta"].get("next_cursor")
            if not cursor:
                break

        db.flush()
        log.info("contacts_synced", ingested=ingested, skipped=skipped)
        return ingested, skipped

    def _upsert_contact(self, db: Session, record: dict) -> bool:
        # Resolve household canonical ID
        hh = household_repo.get_by_source_id(db, record.get("household_id") or "")
        household_id = hh.id if hh else None

        # Store-time policy: hash tax_id, encrypt PII, redact notes
        raw_tax_id = (record.get("tax_id") or "").strip()
        tax_id_hash = self._policy.hash_tax_id(raw_tax_id) if raw_tax_id else None

        raw_email = (record.get("email") or "").strip().lower()
        email_enc = self._policy.encrypt(raw_email) if raw_email else None

        raw_phone = (record.get("phone") or "").strip()
        phone_enc = self._policy.encrypt(raw_phone) if raw_phone else None

        raw_dob = (record.get("date_of_birth") or "").strip()
        dob_enc = self._policy.encrypt(raw_dob) if raw_dob else None

        raw_notes = (record.get("notes") or "").strip()
        redacted_notes = self._policy.redact_free_text(raw_notes) if raw_notes else ""
        notes_enc = self._policy.encrypt(redacted_notes) if redacted_notes else None

        # Normalize account refs — invalid formats are dropped (reconciliation surfaces them)
        raw_refs = record.get("custodian_account_numbers") or []
        crm_account_refs = [
            norm
            for raw in raw_refs
            if (norm := _normalize_account_ref(str(raw))) is not None
        ]

        _, created = person_repo.upsert(
            db,
            source_id=record["id"],
            household_id=household_id,
            first_name=record.get("first_name") or "",
            last_name=record.get("last_name") or "",
            role=record.get("role"),
            tax_id_hash=tax_id_hash,
            email_enc=email_enc,
            phone_enc=phone_enc,
            dob_enc=dob_enc,
            notes_enc=notes_enc,
            crm_account_refs=crm_account_refs,
            updated_at=_parse_crm_dt(record["updated_at"]),
            deleted_at=_parse_crm_dt(record.get("deleted_at")),
        )
        return created

    # ------------------------------------------------------------------ #
    # HTTP helpers                                                         #
    # ------------------------------------------------------------------ #

    def _fetch_page(
        self,
        client: httpx.Client,
        endpoint: str,
        params: dict,
        log,
    ) -> dict:
        url = f"{self._settings.crm_base_url}{endpoint}"
        headers = {"Authorization": f"Bearer {self._settings.crm_token}"}

        def _call() -> dict:
            resp = client.get(url, params=params, headers=headers)
            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", 1.0))
                log.warning("crm_rate_limited", retry_after=retry_after)
                raise RetryableError("CRM rate limited", retry_after=retry_after)
            if resp.status_code == 503:
                log.warning("crm_service_unavailable")
                raise RetryableError("CRM 503 unavailable")
            if resp.status_code == 401:
                raise NonRetryableError("CRM authentication failed (401)")
            if resp.status_code != 200:
                raise NonRetryableError(f"CRM HTTP {resp.status_code} on {endpoint}")
            return resp.json()

        return retry_with_backoff(
            _call,
            max_attempts=self._settings.crm_max_retries,
            base_delay=self._settings.crm_retry_base_delay,
        )


def _update_max_ts(accumulator: list[str], ts: str | None) -> None:
    if not ts:
        return
    if not accumulator or ts > accumulator[0]:
        accumulator[:] = [ts]
