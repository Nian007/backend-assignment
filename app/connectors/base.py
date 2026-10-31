"""
Connector contract — every source connector implements this interface.
Adding a third source = implement ConnectorBase, register in CONNECTORS, add config.
Zero changes to the orchestrator, reconciliation engine, or API routes.
"""
from dataclasses import dataclass, field
from typing import Protocol, ClassVar

from sqlalchemy.orm import Session


@dataclass
class SyncResult:
    source: str
    run_id: str
    records_ingested: int = 0
    records_skipped: int = 0
    errors: list[str] = field(default_factory=list)
    new_checkpoint: str | None = None  # None = do not advance the durable checkpoint


class ConnectorBase(Protocol):
    source_name: ClassVar[str]

    def run(
        self,
        run_id: str,
        db: Session,
        checkpoint: str | None,  # None on the first run (full sync)
    ) -> SyncResult:
        """
        Execute a sync run.

        Contract:
        - Must be idempotent: replaying the same run with the same checkpoint is safe.
        - Must not advance new_checkpoint if the run fails partially.
        - Must not write to canonical tables directly; use repositories.
        - Must not log or surface raw PII values.
        """
        ...
