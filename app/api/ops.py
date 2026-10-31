"""
Operational endpoints — accessible to the 'ops' principal only.

POST /v1/ops/sync/{source}      — trigger a sync run (blocks until complete)
GET  /v1/ops/sync-runs          — list recent sync runs, newest first
GET  /v1/ops/sync-runs/{run_id} — detail for a single run
"""
from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.auth import require_scope, Principal
from app.api.deps import get_db, get_orchestrator
from app.api.errors import APIError
from app.repositories import sync as sync_repo
from app.services.sync_orchestrator import SyncOrchestrator, _SOURCE_ALIASES

router = APIRouter(prefix="/v1/ops", tags=["ops"])


# ------------------------------------------------------------------ #
# Response schemas                                                     #
# ------------------------------------------------------------------ #

class SyncRunOut(BaseModel):
    run_id: str
    source: str
    run_type: str
    status: str
    started_at: datetime
    completed_at: datetime | None
    records_ingested: int
    records_skipped: int
    errors_count: int
    error_detail: list[str] | None
    checkpoint_at: str | None

    model_config = {"from_attributes": True}


def _run_to_out(run) -> SyncRunOut:
    return SyncRunOut(
        run_id=run.id,
        source=run.source,
        run_type=run.run_type,
        status=run.status,
        started_at=run.started_at,
        completed_at=run.completed_at,
        records_ingested=run.records_ingested or 0,
        records_skipped=run.records_skipped or 0,
        errors_count=run.errors_count or 0,
        error_detail=run.error_detail,
        checkpoint_at=run.checkpoint_at,
    )


# ------------------------------------------------------------------ #
# Trigger a sync                                                       #
# ------------------------------------------------------------------ #

@router.post("/sync/{source}", response_model=SyncRunOut)
def trigger_sync(
    source: str,
    principal: Principal = Depends(require_scope("sync:run")),
    db: Session = Depends(get_db),
    orchestrator: SyncOrchestrator = Depends(get_orchestrator),
):
    """
    Trigger a synchronous sync run for the named source.

    Accepted source values: crm, ledgerline_crm, custodian, meridian_custody
    """
    if source not in _SOURCE_ALIASES:
        raise APIError(
            404,
            "unknown_source",
            f"Unknown sync source '{source}'. Valid values: {sorted(_SOURCE_ALIASES)}",
        )
    run = orchestrator.run_sync(source, db)
    return _run_to_out(run)


# ------------------------------------------------------------------ #
# List sync runs                                                       #
# ------------------------------------------------------------------ #

@router.get("/sync-runs", response_model=list[SyncRunOut])
def list_sync_runs(
    source: str | None = None,
    limit: int = 20,
    principal: Principal = Depends(require_scope("sync:read")),
    db: Session = Depends(get_db),
):
    """List recent sync runs, newest first. Optionally filter by source."""
    if limit < 1 or limit > 200:
        raise APIError(400, "invalid_param", "limit must be between 1 and 200.")
    runs = sync_repo.get_recent_runs(db, source=source, limit=limit)
    return [_run_to_out(r) for r in runs]


# ------------------------------------------------------------------ #
# Single run detail                                                    #
# ------------------------------------------------------------------ #

@router.get("/sync-runs/{run_id}", response_model=SyncRunOut)
def get_sync_run(
    run_id: str,
    principal: Principal = Depends(require_scope("sync:read")),
    db: Session = Depends(get_db),
):
    """Return full detail for a single sync run, including error messages."""
    run = sync_repo.get_run_by_id(db, run_id)
    if not run:
        raise APIError(404, "run_not_found", f"Sync run '{run_id}' not found.")
    return _run_to_out(run)
