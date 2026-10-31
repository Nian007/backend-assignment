import uuid
import structlog
import structlog.contextvars
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from app.core.database import init_db
from app.core.settings import get_settings
from app.core.logging import configure_logging

_UI_DIR = Path(__file__).parent / "ui"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)
    init_db()

    # Build shared singletons and store on app.state so dependencies can access them
    from app.policy.engine import PolicyEngine
    from app.services.sync_orchestrator import SyncOrchestrator
    app.state.policy = PolicyEngine(
        policy_path=settings.data_policy_path,
        data_encryption_key=settings.data_encryption_key,
        hmac_secret_key=settings.hmac_secret_key,
    )
    app.state.orchestrator = SyncOrchestrator(policy=app.state.policy)

    structlog.get_logger().info("service_started", database_url=settings.database_url)
    yield
    structlog.get_logger().info("service_stopped")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Household Context Service",
        version="1.0.0",
        description="Canonical household context for Pravar AI agents.",
        lifespan=lifespan,
    )

    # ------------------------------------------------------------------ #
    # Correlation-ID middleware                                            #
    # ------------------------------------------------------------------ #
    @app.middleware("http")
    async def correlation_middleware(request: Request, call_next):
        cid = request.headers.get("X-Correlation-ID") or str(uuid.uuid4())
        request.state.correlation_id = cid
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(correlation_id=cid)
        response = await call_next(request)
        response.headers["X-Correlation-ID"] = cid
        return response

    # ------------------------------------------------------------------ #
    # Error handlers                                                       #
    # ------------------------------------------------------------------ #
    from app.api.errors import APIError, api_error_handler, unhandled_error_handler
    app.add_exception_handler(APIError, api_error_handler)
    app.add_exception_handler(Exception, unhandled_error_handler)

    # ------------------------------------------------------------------ #
    # Routers                                                              #
    # ------------------------------------------------------------------ #
    from app.api.ops import router as ops_router
    app.include_router(ops_router)

    # Context API (Stage 7)
    from app.api.context import router as context_router
    app.include_router(context_router)

    # ------------------------------------------------------------------ #
    # Internal + UI                                                        #
    # ------------------------------------------------------------------ #
    @app.get("/health", tags=["internal"])
    def health():
        return {"status": "ok"}

    # Serve the ops UI at /ui/
    if _UI_DIR.exists():
        @app.get("/ui", tags=["internal"], include_in_schema=False)
        async def ui_redirect():
            from fastapi.responses import RedirectResponse
            return RedirectResponse(url="/ui/index.html")
        app.mount("/ui", StaticFiles(directory=str(_UI_DIR), html=True), name="ui")

    return app


app = create_app()
