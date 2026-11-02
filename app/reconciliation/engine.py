"""
ReconciliationEngine — runs all rules and persists findings.

Usage:
    engine = ReconciliationEngine(policy)
    engine.run(db, sync_run_id=run.id)

The engine:
  1. Calls every registered rule function in order.
  2. Collects the returned Finding objects (already added to the session
     by the rule helpers, but NOT yet committed).
  3. Commits all findings in a single transaction so either all are
     persisted or none are (rollback on error).

Rules are never mixed into connectors or route handlers.
"""
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.policy.engine import PolicyEngine
from app.reconciliation.rules import (
    rule_deleted_household_lifecycle,
    rule_duplicate_positions,
    rule_missing_crm_household,
    rule_position_balance_mismatch,
    rule_possible_duplicate_persons,
    rule_unlinked_accounts,
)

_RULES = [
    rule_possible_duplicate_persons,
    rule_missing_crm_household,
    rule_unlinked_accounts,
    rule_deleted_household_lifecycle,
    rule_duplicate_positions,
    rule_position_balance_mismatch,
]


class ReconciliationEngine:
    def __init__(self, policy: PolicyEngine) -> None:
        self._policy = policy
        self._log = get_logger("reconciliation_engine")

    def run(self, db: Session, sync_run_id: str | None = None) -> int:
        """
        Run all reconciliation rules and commit findings.

        Each run replaces previous findings to prevent duplicates.
        Returns the number of findings emitted.
        """
        log = self._log.bind(sync_run_id=sync_run_id)
        log.info("reconciliation_started", rules=len(_RULES))

        from app.models.canonical import Finding
        db.query(Finding).delete()
        db.flush()

        all_findings = []
        for rule in _RULES:
            try:
                findings = rule(db, sync_run_id, self._policy)
                all_findings.extend(findings)
                log.info(
                    "rule_complete",
                    rule=rule.__name__,
                    findings=len(findings),
                )
            except Exception as exc:  # noqa: BLE001
                log.error(
                    "rule_error",
                    rule=rule.__name__,
                    error_type=type(exc).__name__,
                )
                db.rollback()
                raise

        try:
            db.commit()
        except Exception:
            db.rollback()
            raise

        log.info("reconciliation_complete", total_findings=len(all_findings))
        return len(all_findings)
