import uuid
import structlog
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import structlog.contextvars

from app.core.database import init_db
from app.core.settings import get_settings
from app.core.logging import configure_logging


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)
    init_db()
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

    @app.middleware("http")
    async def correlation_middleware(request: Request, call_next):
        correlation_id = request.headers.get("X-Correlation-ID") or str(uuid.uuid4())
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(correlation_id=correlation_id)
        response = await call_next(request)
        response.headers["X-Correlation-ID"] = correlation_id
        return response

    # Routers registered in later stages
    # from app.api.context import router as context_router
    # from app.api.ops import router as ops_router
    # app.include_router(context_router)
    # app.include_router(ops_router)

    @app.get("/health", tags=["internal"])
    def health():
        return {"status": "ok"}

    return app


app = create_app()
