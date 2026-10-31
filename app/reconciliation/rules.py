"""
Reconciliation rule definitions.

Each rule is a pure function:
    rule(db, sync_run_id, policy) -> list[Finding]

The ReconciliationEngine collects all findings, persists them, and
commits them in a single transaction after all rules have run.

Severity model
--------------
CRITICAL  Account-level data that makes the household total unreliable
          and should block an AI agent from quoting numbers.
HIGH      Material data-quality problem that degrades the household
          context but does not make it wholly unusable.
MEDIUM    Data that is incomplete or ambiguous; agent can proceed with
          a caveat.
LOW       Informational anomaly; no material impact on context.

These severities are checked in the context assembler to compute
data_quality.status (unreliable / degraded / ok).
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from app.models.canonical import (
    Account, CustodianDrop, Finding, Household,
    Person, Position, Valuation,
)
from app.repositories import findings as findings_repo

if TYPE_CHECKING:
    from app.policy.engine import PolicyEngine


# ------------------------------------------------------------------ #
# Severity constants                                                   #
# ------------------------------------------------------------------ #

CRITICAL = "critical"
HIGH     = "high"
MEDIUM   = "medium"
LOW      = "low"


# ------------------------------------------------------------------ #
# Helper                                                               #
# ------------------------------------------------------------------ #

def _emit(
    db: Session,
    sync_run_id: str | None,
    code: str,
    severity: str,
    message: str,
    *,
    entity_type: str | None = None,
    entity_id: str | None = None,
    source: str | None = None,
    drop_id: str | None = None,
    expected: str | None = None,
    observed: str | None = None,
) -> Finding:
    return findings_repo.create(
        db,
        code=code,
        severity=severity,
        message=message,
        entity_type=entity_type,
        entity_id=entity_id,
        source=source,
        drop_id=drop_id,
        sync_run_id=sync_run_id,
        expected_value=expected,
        observed_value=observed,
    )


# ------------------------------------------------------------------ #
# Rule 1 — Possible duplicate persons                                 #
# ------------------------------------------------------------------ #

def rule_possible_duplicate_persons(
    db: Session,
    sync_run_id: str | None,
    policy: "PolicyEngine",
) -> list[Finding]:
    """
    Detect pairs of persons in the same household that share strong
    identity signals (hashed tax_id match).

    We compare tax_id_hash values (keyed HMACs) — the raw values are
    never available at this layer.  A matching hash is a strong signal.

    Do NOT log or include the matching values in the finding message.
    """
    findings: list[Finding] = []

    # Group persons by household
    persons = db.query(Person).filter(
        Person.deleted_at.is_(None),
        Person.household_id.isnot(None),
    ).all()

    by_household: dict[str, list[Person]] = defaultdict(list)
    for p in persons:
        by_household[p.household_id].append(p)

    already_flagged: set[frozenset[str]] = set()

    for _hh_id, members in by_household.items():
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                pair = frozenset({a.id, b.id})
                if pair in already_flagged:
                    continue

                signals = 0
                # Tax-ID hash match (strongest signal)
                if a.tax_id_hash and b.tax_id_hash and a.tax_id_hash == b.tax_id_hash:
                    signals += 2

                # Similar name (first+last lowercase)
                a_name = f"{a.first_name.lower()} {a.last_name.lower()}"
                b_name = f"{b.first_name.lower()} {b.last_name.lower()}"
                if a_name == b_name:
                    signals += 1
                # One name is prefix/nickname of the other (e.g. Bob/Robert)
                elif (a.last_name.lower() == b.last_name.lower()
                      and (a.first_name.lower() in b.first_name.lower()
                           or b.first_name.lower() in a.first_name.lower())):
                    signals += 1

                if signals >= 2:
                    already_flagged.add(pair)
                    f = _emit(
                        db,
                        sync_run_id,
                        code="POSSIBLE_DUPLICATE_PERSON",
                        severity=MEDIUM,
                        message=(
                            f"Persons {a.source_id} and {b.source_id} in the same household "
                            f"share strong identity signals. Retain separately until manually resolved."
                        ),
                        entity_type="person",
                        entity_id=a.id,
                        source="ledgerline_crm",
                    )
                    findings.append(f)

    return findings


# ------------------------------------------------------------------ #
# Rule 2 — Missing CRM household                                      #
# ------------------------------------------------------------------ #

def rule_missing_crm_household(
    db: Session,
    sync_run_id: str | None,
    policy: "PolicyEngine",
) -> list[Finding]:
    """Contacts that reference a household_id not in the canonical store."""
    findings: list[Finding] = []

    all_hh_source_ids = {hh.source_id for hh in db.query(Household.source_id).all()}

    orphan_persons = (
        db.query(Person)
        .filter(Person.household_id.is_(None), Person.deleted_at.is_(None))
        .all()
    )

    for p in orphan_persons:
        f = _emit(
            db,
            sync_run_id,
            code="MISSING_CRM_HOUSEHOLD",
            severity=HIGH,
            message=(
                f"Person {p.source_id} references a household that is not in the canonical "
                f"store. The contact was retained; no household was fabricated."
            ),
            entity_type="person",
            entity_id=p.id,
            source="ledgerline_crm",
        )
        findings.append(f)

    return findings


# ------------------------------------------------------------------ #
# Rule 3 — Unlinked custodian accounts                                #
# ------------------------------------------------------------------ #

def rule_unlinked_accounts(
    db: Session,
    sync_run_id: str | None,
    policy: "PolicyEngine",
) -> list[Finding]:
    """
    Custodian accounts with no CRM household association.

    These must not contribute to household totals.
    """
    findings: list[Finding] = []

    unlinked = (
        db.query(Account)
        .filter(Account.household_id.is_(None))
        .all()
    )

    for acc in unlinked:
        f = _emit(
            db,
            sync_run_id,
            code="UNLINKED_ACCOUNT",
            severity=MEDIUM,
            message=(
                f"Account ending {acc.account_number[-4:]} has no confirmed CRM household "
                f"association. It is excluded from all household totals until resolved."
            ),
            entity_type="account",
            entity_id=acc.id,
            source="meridian_custody",
            drop_id=None,
        )
        findings.append(f)

    return findings


# ------------------------------------------------------------------ #
# Rule 4 — Deleted household with linked accounts                     #
# ------------------------------------------------------------------ #

def rule_deleted_household_lifecycle(
    db: Session,
    sync_run_id: str | None,
    policy: "PolicyEngine",
) -> list[Finding]:
    """
    A soft-deleted CRM household that still has open custodian accounts
    is a lifecycle discrepancy.
    """
    findings: list[Finding] = []

    deleted_households = (
        db.query(Household)
        .filter(Household.deleted_at.isnot(None))
        .all()
    )

    for hh in deleted_households:
        open_accounts = [
            acc for acc in hh.accounts
            if acc.status.upper() == "OPEN"
        ]
        for acc in open_accounts:
            f = _emit(
                db,
                sync_run_id,
                code="DELETED_HOUSEHOLD_OPEN_ACCOUNT",
                severity=MEDIUM,
                message=(
                    f"Household {hh.source_id} is soft-deleted in CRM but account "
                    f"ending {acc.account_number[-4:]} is still OPEN in the custodian."
                ),
                entity_type="account",
                entity_id=acc.id,
                source="ledgerline_crm",
                expected="account CLOSED or household active",
                observed="household deleted, account OPEN",
            )
            findings.append(f)

    return findings


# ------------------------------------------------------------------ #
# Rule 5 — Duplicate positions                                        #
# ------------------------------------------------------------------ #

def _effective_drop_ids(db: Session) -> set[str]:
    """Return canonical DB-PKs for all effective (non-superseded) drops."""
    all_drops = db.query(CustodianDrop).all()
    superseded_drop_ids = {d.supersedes for d in all_drops if d.supersedes}
    effective: set[str] = set()
    for d in all_drops:
        if d.drop_id not in superseded_drop_ids and d.status in ("ingested", "accepted"):
            effective.add(d.id)
    return effective


def rule_duplicate_positions(
    db: Session,
    sync_run_id: str | None,
    policy: "PolicyEngine",
) -> list[Finding]:
    """
    Detect duplicate position rows in effective drops.

    Duplicates: same (account_id, as_of_date, symbol, cusip).
    Do not silently deduplicate — emit a finding per duplicate group.
    """
    findings: list[Finding] = []
    effective_ids = _effective_drop_ids(db)

    positions = (
        db.query(Position)
        .filter(Position.drop_id.in_(effective_ids))
        .all()
    )

    # Group by (account_id, as_of_date, symbol, cusip)
    groups: dict[tuple, list[Position]] = defaultdict(list)
    for pos in positions:
        key = (pos.account_id, str(pos.as_of_date), pos.symbol, pos.cusip or "")
        groups[key].append(pos)

    for (account_id, as_of, symbol, cusip), dupes in groups.items():
        if len(dupes) <= 1:
            continue
        acc = db.get(Account, account_id)
        acct_suffix = acc.account_number[-4:] if acc else "????"
        f = _emit(
            db,
            sync_run_id,
            code="DUPLICATE_POSITION",
            severity=HIGH,
            message=(
                f"Account ending {acct_suffix}: {len(dupes)} duplicate {symbol} position rows "
                f"for {as_of}. Sum of market values may be inflated. Manual review required."
            ),
            entity_type="account",
            entity_id=account_id,
            source="meridian_custody",
            observed=f"{len(dupes)} rows for ({symbol}, {as_of})",
            expected="1 row",
        )
        findings.append(f)

    return findings


# ------------------------------------------------------------------ #
# Rule 6 — Position sum vs account balance                            #
# ------------------------------------------------------------------ #

_TOLERANCE = Decimal("0.05")   # $0.05 tolerance for floating-point CSVs


def rule_position_balance_mismatch(
    db: Session,
    sync_run_id: str | None,
    policy: "PolicyEngine",
) -> list[Finding]:
    """
    For each account in each effective drop, compare the sum of position
    market values against the custodian's reported total_market_value.

    Cash is represented both as a CASH position AND in cash_balance.
    To avoid double-counting, sum only non-CASH positions, then add
    cash_balance (from the Valuation row) for the total.

    If the custodian's total_market_value differs by more than $0.05
    from (sum of non-CASH positions + cash_balance), emit a finding.
    """
    findings: list[Finding] = []
    effective_ids = _effective_drop_ids(db)

    valuations = (
        db.query(Valuation)
        .filter(Valuation.drop_id.in_(effective_ids))
        .all()
    )

    for val in valuations:
        try:
            reported_total = Decimal(val.total_market_value)
            reported_cash = Decimal(val.cash_balance)
        except InvalidOperation:
            continue

        positions = (
            db.query(Position)
            .filter(
                Position.account_id == val.account_id,
                Position.drop_id == val.drop_id,
                Position.as_of_date == val.as_of_date,
            )
            .all()
        )

        # Sum non-CASH positions; CASH position would duplicate cash_balance
        non_cash_sum = Decimal("0")
        for pos in positions:
            if pos.asset_class.upper() == "CASH":
                continue
            try:
                non_cash_sum += Decimal(pos.market_value)
            except InvalidOperation:
                continue

        computed_total = non_cash_sum + reported_cash
        diff = abs(computed_total - reported_total)

        if diff > _TOLERANCE:
            acc = db.get(Account, val.account_id)
            acct_suffix = acc.account_number[-4:] if acc else "????"
            severity = HIGH if diff > Decimal("1.00") else LOW
            f = _emit(
                db,
                sync_run_id,
                code="POSITION_SUM_MISMATCH",
                severity=severity,
                message=(
                    f"Account ending {acct_suffix} ({val.as_of_date}): "
                    f"computed total ({computed_total}) differs from "
                    f"custodian total ({reported_total}) by {diff}."
                ),
                entity_type="account",
                entity_id=val.account_id,
                source="meridian_custody",
                drop_id=val.drop_id,
                expected=str(reported_total),
                observed=str(computed_total),
            )
            findings.append(f)

    return findings
