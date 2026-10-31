"""
Agent Context API — GET /v1/households/{household_id}/context

This module is the entry point for Stage 7 implementation.
The full context assembly, policy-filtered egress, and data_quality
status computation are implemented in the ContextAssembler service.
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.auth import Principal, require_scope
from app.api.deps import get_db, get_policy
from app.api.errors import APIError
from app.repositories import sync as sync_repo

router = APIRouter(prefix="/v1/households", tags=["context"])


@router.get("/{household_id}/context")
def get_household_context(
    household_id: str,
    principal: Principal = Depends(require_scope("context:read")),
    db: Session = Depends(get_db),
    policy=Depends(get_policy),
):
    """
    Return the canonical household context for AI agents and ops.

    The response is filtered per principal (agent vs ops) according
    to the data policy defined in contracts/data_policy.yaml.
    """
    # Gate: require at least one successful sync before serving context
    if not sync_repo.has_any_successful_run(db):
        raise APIError(
            503,
            "context_unavailable",
            "No successful synchronization has completed yet. "
            "Trigger a sync via POST /v1/ops/sync/crm and POST /v1/ops/sync/custodian.",
        )

    from app.services.context_assembler import ContextAssembler
    assembler = ContextAssembler(policy=policy)
    return assembler.build(household_id=household_id, principal=principal, db=db)
