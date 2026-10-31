"""
Shared FastAPI dependencies.

- get_db           : re-exports the DB session generator from core.database
- get_policy       : returns the app-level PolicyEngine (from app.state)
- get_orchestrator : returns the app-level SyncOrchestrator (from app.state)
"""
from fastapi import Request

from app.core.database import get_db  # noqa: re-exported for routes to import from one place

__all__ = ["get_db", "get_policy", "get_orchestrator"]


def get_policy(request: Request):
    return request.app.state.policy


def get_orchestrator(request: Request):
    return request.app.state.orchestrator
