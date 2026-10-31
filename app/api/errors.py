"""
Canonical error format for the API.

All error responses follow the exact shape:
  {"error": {"code": "...", "message": "...", "correlation_id": "..."}}

Raise APIError anywhere in a route; the handler in main.py formats the response.
"""
from fastapi import Request
from fastapi.responses import JSONResponse
import structlog.contextvars


class APIError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message


def _get_cid(request: Request) -> str:
    return getattr(request.state, "correlation_id", "unknown")


async def api_error_handler(request: Request, exc: APIError) -> JSONResponse:
    cid = _get_cid(request)
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message, "correlation_id": cid}},
        headers={"X-Correlation-ID": cid},
    )


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    cid = _get_cid(request)
    structlog.get_logger().error("unhandled_exception", error_type=type(exc).__name__)
    return JSONResponse(
        status_code=500,
        content={"error": {"code": "internal_error",
                           "message": "An unexpected error occurred.",
                           "correlation_id": cid}},
        headers={"X-Correlation-ID": cid},
    )
