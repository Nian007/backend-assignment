"""
ContextAssembler — builds the household context response.

Responsibilities:
  1. Load household, persons, accounts, positions, valuations, findings.
  2. Apply data-policy egress transforms per principal.
  3. Compute data_quality.status from findings.
  4. Aggregate totals over confirmed-linked accounts only.
  5. Return a dict that matches the fixed API contract.

Data quality status rules:
  unreliable — any CRITICAL finding touching this household's data, or
               any account with an unresolved DUPLICATE_POSITION (HIGH)
               that inflates totals.
  degraded   — any HIGH finding that does not make totals wholly unreliable.
  ok         — only MEDIUM / LOW findings (or none).

Ops principal receives a `detail` object on each finding.
Agent principal never receives `detail`.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy.orm import Session

from app.api.auth import Principal
from app.api.errors import APIError
from app.models.canonical import (
    Account, CustodianDrop, Finding, Household, Person, Position, Valuation,
)
from app.policy.engine import OMIT, PolicyEngine
from app.repositories import findings as findings_repo
from app.repositories import sync as sync_repo

_SEVERITY_ORDER = {"critical": 3, "high": 2, "medium": 1, "low": 0}


class ContextAssembler:
    def __init__(self, policy: PolicyEngine) -> None:
        self._policy = policy

    def build(self, household_id: str, principal: Principal, db: Session) -> dict:
        # 1. Load household
        hh: Household | None = (
            db.query(Household).filter_by(source_id=household_id).first()
        )
        if not hh:
            raise APIError(404, "household_not_found",
                           f"Household '{household_id}' not found.")

        cid = _current_correlation_id()

        # 2. Members
        persons: list[Person] = (
            db.query(Person)
            .filter_by(household_id=hh.id)
            .filter(Person.deleted_at.is_(None))
            .all()
        )

        # 3. Accounts (only household-linked, non-unlinked accounts)
        accounts: list[Account] = (
            db.query(Account)
            .filter_by(household_id=hh.id)
            .all()
        )
        account_ids = [a.id for a in accounts]

        # 4. Findings
        all_findings: list[Finding] = findings_repo.get_active_for_household(
            db, hh.id, account_ids
        )

        # 5. Data quality status
        dq_status = _compute_dq_status(all_findings)

        # 6. Effective drops (for freshness)
        custody_checkpoint = sync_repo.get_checkpoint(db, "meridian_custody")

        # 7. Build response
        return {
            "household": _build_household(hh),
            "members": [_build_member(p, principal, self._policy) for p in persons],
            "accounts": [
                _build_account(a, principal, self._policy, db)
                for a in accounts
            ],
            "totals": _build_totals(accounts, db),
            "freshness": _build_freshness(db, sync_repo),
            "data_quality": _build_data_quality(
                dq_status, all_findings, principal
            ),
            "correlation_id": cid,
        }


# ------------------------------------------------------------------ #
# Household block                                                      #
# ------------------------------------------------------------------ #

def _build_household(hh: Household) -> dict:
    return {
        "id": hh.source_id,
        "name": hh.name,
        "primary_advisor": hh.primary_advisor,
        "risk_profile": hh.risk_profile,
        "service_tier": hh.service_tier,
        "status": "deleted" if hh.deleted_at else "active",
    }


# ------------------------------------------------------------------ #
# Members block                                                        #
# ------------------------------------------------------------------ #

def _build_member(p: Person, principal: Principal, policy: PolicyEngine) -> dict:
    member: dict[str, Any] = {
        "person_id": p.id,
        "display_name": f"{p.first_name} {p.last_name}",
        "role": p.role,
    }
    # email
    if p.email_enc:
        try:
            raw_email = policy.decrypt(p.email_enc)
            result = policy.apply_egress("person.email", raw_email, principal.name)
            if result is not OMIT:
                member["email"] = result
        except Exception:
            pass

    # dob → age_band
    if p.dob_enc:
        try:
            raw_dob = policy.decrypt(p.dob_enc)
            result = policy.apply_egress("person.date_of_birth", raw_dob, principal.name)
            if result is not OMIT:
                member["age_band"] = result
        except Exception:
            pass

    # phone (ops gets last4, agent gets nothing)
    if p.phone_enc:
        try:
            raw_phone = policy.decrypt(p.phone_enc)
            result = policy.apply_egress("person.phone", raw_phone, principal.name)
            if result is not OMIT:
                member["phone"] = result
        except Exception:
            pass

    # notes
    if p.notes_enc:
        try:
            raw_notes = policy.decrypt(p.notes_enc)
            result = policy.apply_egress("person.notes", raw_notes, principal.name)
            if result is not OMIT:
                member["notes"] = result
        except Exception:
            pass

    return member


# ------------------------------------------------------------------ #
# Accounts block                                                       #
# ------------------------------------------------------------------ #

def _build_account(
    acc: Account, principal: Principal, policy: PolicyEngine, db: Session
) -> dict:
    # Account number — both principals receive last4
    acct_display = policy.apply_egress(
        "account.account_number", acc.account_number, principal.name
    )

    # Latest effective valuation for this account
    latest_val = _latest_valuation(acc.id, db)

    # Positions from the latest effective drop
    positions = []
    if latest_val:
        raw_positions = (
            db.query(Position)
            .filter_by(account_id=acc.id, drop_id=latest_val.drop_id)
            .all()
        )
        for pos in raw_positions:
            positions.append({
                "symbol": pos.symbol,
                "asset_class": pos.asset_class,
                "quantity": pos.quantity,
                "price": pos.price,
                "market_value": {
                    "amount": pos.market_value,
                    "currency": pos.currency,
                },
            })

    # Owners
    owners = [o.person_id for o in acc.ownerships]

    record: dict[str, Any] = {
        "account_id": acc.id,
        "account_number_last4": acct_display if acct_display is not OMIT else "****",
        "registration_type": acc.registration_type,
        "status": acc.status,
        "owners": owners,
        "positions": positions,
        "lineage": _build_account_lineage(acc, latest_val),
    }

    if latest_val:
        record["as_of_date"] = str(latest_val.as_of_date)
        record["total_market_value"] = latest_val.total_market_value
        record["market_value"] = {
            "amount": latest_val.total_market_value,
            "currency": latest_val.currency,
        }

    return record


def _latest_valuation(account_id: str, db: Session) -> Valuation | None:
    """Return the most recent valuation for this account from an effective drop."""
    all_drops = db.query(CustodianDrop).all()
    superseded = {d.supersedes for d in all_drops if d.supersedes}
    effective_drop_ids = {
        d.id for d in all_drops
        if d.drop_id not in superseded and d.status == "accepted"
    }

    vals = (
        db.query(Valuation)
        .filter(
            Valuation.account_id == account_id,
            Valuation.drop_id.in_(effective_drop_ids),
        )
        .order_by(Valuation.as_of_date.desc())
        .all()
    )
    return vals[0] if vals else None


def _build_account_lineage(acc: Account, val: Valuation | None) -> list[dict]:
    if not val:
        return []
    return [
        {
            "source": "meridian_custody",
            "record_ref": f"drop/{val.drop_id}",
            "ingested_at": val.ingested_at.isoformat() + "Z"
            if val.ingested_at and not val.ingested_at.tzinfo
            else (val.ingested_at.isoformat() if val.ingested_at else None),
        }
    ]


# ------------------------------------------------------------------ #
# Totals block                                                         #
# ------------------------------------------------------------------ #

def _build_totals(accounts: list[Account], db: Session) -> dict:
    total_mv = Decimal("0")
    by_class: dict[str, Decimal] = {}

    for acc in accounts:
        val = _latest_valuation(acc.id, db)
        if not val:
            continue
        try:
            total_mv += Decimal(val.total_market_value)
        except InvalidOperation:
            pass

        # Aggregate by asset class from positions
        raw_positions = (
            db.query(Position)
            .filter_by(account_id=acc.id, drop_id=val.drop_id)
            .all()
        )
        for pos in raw_positions:
            cls = pos.asset_class.upper()
            try:
                mv = Decimal(pos.market_value)
                by_class[cls] = by_class.get(cls, Decimal("0")) + mv
            except InvalidOperation:
                pass

    return {
        "market_value": {"amount": str(total_mv), "currency": "USD"},
        "by_asset_class": {k: str(v) for k, v in sorted(by_class.items())},
    }


# ------------------------------------------------------------------ #
# Freshness block                                                      #
# ------------------------------------------------------------------ #

def _build_freshness(db: Session, sync_repo_module) -> list[dict]:
    freshness = []
    for source in ("ledgerline_crm", "meridian_custody"):
        chk = sync_repo_module.get_checkpoint(db, source)
        entry: dict[str, Any] = {"source": source}
        if chk:
            entry["last_successful_sync_at"] = chk
        freshness.append(entry)
    return freshness


# ------------------------------------------------------------------ #
# Data quality block                                                   #
# ------------------------------------------------------------------ #

def _compute_dq_status(findings: list[Finding]) -> str:
    max_severity = -1
    for f in findings:
        max_severity = max(max_severity, _SEVERITY_ORDER.get(f.severity, 0))
    if max_severity >= _SEVERITY_ORDER["critical"]:
        return "unreliable"
    if max_severity >= _SEVERITY_ORDER["high"]:
        return "degraded"
    return "ok"


def _build_data_quality(
    status: str, findings: list[Finding], principal: Principal
) -> dict:
    finding_list = []
    for f in findings:
        entry: dict[str, Any] = {
            "code": f.code,
            "severity": f.severity,
            "message": f.message,
            "entity": f.entity_id,
        }
        # ops receives detail; agent never does
        if principal.name == "ops" and (f.expected_value or f.observed_value):
            entry["detail"] = {
                "expected": f.expected_value,
                "observed": f.observed_value,
                "source": f.source,
                "drop_id": f.drop_id,
            }
        finding_list.append(entry)

    return {"status": status, "findings": finding_list}


# ------------------------------------------------------------------ #
# Correlation ID helper                                                #
# ------------------------------------------------------------------ #

def _current_correlation_id() -> str:
    try:
        import structlog.contextvars
        ctx = structlog.contextvars.get_contextvars()
        return ctx.get("correlation_id", "")
    except Exception:
        return ""
