"""
SyncOrchestrator — coordinates connector execution, sync-run lifecycle,
and durable checkpoint advancement.

Design rules:
  - The SyncRun record is committed at creation so it is visible even if the
    process crashes during the sync.
  - The checkpoint advances ONLY when the connector reports no errors.
    Partial failures leave the checkpoint unchanged so the next run replays
    from the last safe position.
  - Connectors are idempotent, so replaying already-processed records is safe.
  - A failed sync does NOT erase previously ingested canonical data.
"""
from sqlalchemy.orm import Session

from app.connectors.crm import CRMConnector
from app.connectors.custodian import CustodianConnector
from app.core.logging import get_logger
from app.models.canonical import SyncRun
from app.policy.engine import PolicyEngine
from app.repositories import sync as sync_repo

_SOURCE_ALIASES: dict[str, str] = {
    "crm": "ledgerline_crm",
    "ledgerline_crm": "ledgerline_crm",
    "custodian": "meridian_custody",
    "meridian_custody": "meridian_custody",
}

VALID_SOURCES = set(_SOURCE_ALIASES.values())


class SyncOrchestrator:
    def __init__(self, policy: PolicyEngine) -> None:
        self._policy = policy
        self._log = get_logger("sync_orchestrator")

    def run_sync(self, source_alias: str, db: Session) -> SyncRun:
        """
        Run a full or incremental sync for `source_alias`.

        Returns the SyncRun record (already committed).
        Raises ValueError for unknown source aliases.
        """
        source = _SOURCE_ALIASES.get(source_alias)
        if not source:
            raise ValueError(
                f"Unknown source '{source_alias}'. Valid: {sorted(_SOURCE_ALIASES)}"
            )

        checkpoint = sync_repo.get_checkpoint(db, source)
        run_type = "incremental" if checkpoint else "full"
        run = sync_repo.create_run(db, source=source, run_type=run_type)

        log = self._log.bind(run_id=run.id, source=source, run_type=run_type)
        log.info("sync_run_started", checkpoint=checkpoint)

        connector = self._build_connector(source)

        try:
            result = connector.run(run_id=run.id, db=db, checkpoint=checkpoint)
        except Exception as exc:  # noqa: BLE001
            log.error("connector_raised", error_type=type(exc).__name__)
            sync_repo.fail_run(db, run, errors=[f"connector error: {type(exc).__name__}"])
            return run

        if result.errors:
            # Partial or total failure — do NOT advance checkpoint
            log.warning("sync_run_failed",
                        errors=len(result.errors),
                        ingested=result.records_ingested)
            sync_repo.fail_run(db, run, errors=result.errors)
        else:
            # Full success — advance checkpoint then reconcile
            if result.new_checkpoint:
                sync_repo.set_checkpoint(db, source, result.new_checkpoint)
            sync_repo.complete_run(
                db,
                run,
                records_ingested=result.records_ingested,
                records_skipped=result.records_skipped,
                errors=[],
                checkpoint_at=result.new_checkpoint,
            )
            log.info("sync_run_complete",
                     ingested=result.records_ingested,
                     skipped=result.records_skipped,
                     new_checkpoint=result.new_checkpoint)
            # Run reconciliation after every successful sync
            try:
                from app.reconciliation.engine import ReconciliationEngine
                rec_engine = ReconciliationEngine(policy=self._policy)
                n_findings = rec_engine.run(db, sync_run_id=run.id)
                log.info("reconciliation_done", findings=n_findings)
            except Exception as exc:  # noqa: BLE001 — reconciliation failure must not fail the sync
                log.error("reconciliation_error", error_type=type(exc).__name__)

        db.refresh(run)
        return run

    def _build_connector(self, source: str):
        if source == "ledgerline_crm":
            return CRMConnector(policy=self._policy)
        if source == "meridian_custody":
            return CustodianConnector()
        raise ValueError(f"No connector registered for source '{source}'")
