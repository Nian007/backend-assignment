"""
Mock CRM ("Ledgerline CRM") for the Pravar backend assignment.

Behaves like a real third-party CRM API, including the parts that make integration hard:
bearer auth, cursor pagination, an inclusive updated_since filter, rate limiting with
Retry-After, intermittent 5xx errors, and soft deletes exposed only on request.

Run:   uvicorn mock_crm.server:app --port 8081
Docs:  http://localhost:8081/docs   (see also contracts/crm-api.md)

Environment variables
  CRM_TOKEN          bearer token accepted by the API       (default: pravar-dev-token)
  CRM_FAULT_RATE     probability of a 503 on any data call   (default: 0.08)
  CRM_RATE_LIMIT     requests allowed per 10 second window   (default: 10)
  CRM_SEED           RNG seed for fault injection            (default: unset, i.e. random)
"""
from __future__ import annotations

import base64
import copy
import json
import os
import random
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse

SEED_DIR = Path(__file__).parent / "seed"
TOKEN = os.getenv("CRM_TOKEN", "pravar-dev-token")
FAULT_RATE = float(os.getenv("CRM_FAULT_RATE", "0.08"))
RATE_LIMIT = int(os.getenv("CRM_RATE_LIMIT", "10"))
WINDOW_S = 10
MAX_LIMIT = 50

_rng = random.Random(int(os.environ["CRM_SEED"])) if os.getenv("CRM_SEED") else random.Random()
_lock = threading.Lock()
_hits: dict[str, list[float]] = {}


def _load() -> dict[str, list[dict]]:
    return {
        "households": json.loads((SEED_DIR / "households.json").read_text()),
        "contacts": json.loads((SEED_DIR / "contacts.json").read_text()),
    }


STORE = _load()

app = FastAPI(
    title="Ledgerline CRM (mock)",
    version="2.3.0",
    description="Simulated third-party CRM for the Pravar backend assignment.",
)


# ------------------------------------------------------------------ helpers
def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _auth(authorization: Optional[str]) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, detail={"error": "unauthorized", "message": "Missing bearer token"})
    tok = authorization.removeprefix("Bearer ").strip()
    if tok != TOKEN:
        raise HTTPException(401, detail={"error": "unauthorized", "message": "Invalid token"})
    return tok


def _rate_limit(tok: str) -> None:
    now = time.monotonic()
    with _lock:
        hits = [t for t in _hits.get(tok, []) if now - t < WINDOW_S]
        if len(hits) >= RATE_LIMIT:
            retry = max(1, int(WINDOW_S - (now - hits[0])) + 1)
            _hits[tok] = hits
            raise HTTPException(
                429,
                detail={"error": "rate_limited", "message": "Too many requests"},
                headers={"Retry-After": str(retry)},
            )
        hits.append(now)
        _hits[tok] = hits


def _maybe_fail() -> None:
    if _rng.random() < FAULT_RATE:
        raise HTTPException(503, detail={"error": "upstream_unavailable", "message": "Try again later"})


def _encode_cursor(offset: int, snapshot: str) -> str:
    return base64.urlsafe_b64encode(json.dumps({"o": offset, "s": snapshot}).encode()).decode()


def _decode_cursor(cursor: str) -> tuple[int, str]:
    try:
        d = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
        return int(d["o"]), str(d["s"])
    except Exception:
        raise HTTPException(400, detail={"error": "invalid_cursor", "message": "Cursor is malformed"})


def _page(collection: str, updated_since: Optional[str], include_deleted: bool,
          cursor: Optional[str], limit: int) -> dict:
    limit = max(1, min(limit, MAX_LIMIT))
    rows = copy.deepcopy(STORE[collection])
    if updated_since:
        try:
            datetime.strptime(updated_since, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            raise HTTPException(400, detail={"error": "invalid_parameter",
                                             "message": "updated_since must be YYYY-MM-DDTHH:MM:SSZ"})
        # NOTE: inclusive comparison, as documented.
        rows = [r for r in rows if r["updated_at"] >= updated_since]
    if not include_deleted:
        rows = [r for r in rows if not r.get("deleted_at")]
    rows.sort(key=lambda r: (r["updated_at"], r["id"]))

    offset, snapshot = (0, _now()) if not cursor else _decode_cursor(cursor)
    chunk = rows[offset: offset + limit]
    nxt = offset + limit
    return {
        "data": chunk,
        "meta": {
            "count": len(chunk),
            "next_cursor": _encode_cursor(nxt, snapshot) if nxt < len(rows) else None,
            "server_time": _now(),
        },
    }


@app.exception_handler(HTTPException)
async def _http_exc(_: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content=exc.detail, headers=exc.headers)


# ------------------------------------------------------------------ public API
@app.get("/v2/households")
def list_households(
    authorization: Optional[str] = Header(None),
    updated_since: Optional[str] = Query(None, description="Inclusive. ISO-8601 UTC, seconds precision."),
    include_deleted: bool = Query(False, description="Return soft-deleted records (tombstones)."),
    cursor: Optional[str] = None,
    limit: int = Query(25, ge=1, le=MAX_LIMIT),
):
    tok = _auth(authorization)
    _rate_limit(tok)
    _maybe_fail()
    return _page("households", updated_since, include_deleted, cursor, limit)


@app.get("/v2/contacts")
def list_contacts(
    authorization: Optional[str] = Header(None),
    updated_since: Optional[str] = Query(None, description="Inclusive. ISO-8601 UTC, seconds precision."),
    include_deleted: bool = Query(False),
    cursor: Optional[str] = None,
    limit: int = Query(25, ge=1, le=MAX_LIMIT),
):
    tok = _auth(authorization)
    _rate_limit(tok)
    _maybe_fail()
    return _page("contacts", updated_since, include_deleted, cursor, limit)


@app.get("/v2/contacts/{contact_id}")
def get_contact(contact_id: str, authorization: Optional[str] = Header(None)):
    tok = _auth(authorization)
    _rate_limit(tok)
    _maybe_fail()
    for c in STORE["contacts"]:
        if c["id"] == contact_id and not c.get("deleted_at"):
            return {"data": c}
    raise HTTPException(404, detail={"error": "not_found", "message": f"{contact_id} not found"})


# ------------------------------------------------------------------ test hooks
# Not part of the "vendor" API. Use these to exercise incremental sync locally.
@app.post("/_admin/contacts/{contact_id}/touch", include_in_schema=True, tags=["test hooks"])
def touch_contact(contact_id: str, field: str = "notes", value: str = "Updated via test hook"):
    for c in STORE["contacts"]:
        if c["id"] == contact_id:
            c[field] = value
            c["updated_at"] = _now()
            return {"data": c}
    raise HTTPException(404, detail={"error": "not_found"})


@app.post("/_admin/contacts/{contact_id}/delete", tags=["test hooks"])
def soft_delete_contact(contact_id: str):
    for c in STORE["contacts"]:
        if c["id"] == contact_id:
            c["deleted_at"] = c["updated_at"] = _now()
            return {"data": c}
    raise HTTPException(404, detail={"error": "not_found"})


@app.post("/_admin/reset", tags=["test hooks"])
def reset():
    global STORE
    STORE = _load()
    with _lock:
        _hits.clear()
    return {"status": "reset"}


@app.get("/health", tags=["test hooks"])
def health():
    return {"status": "ok", "fault_rate": FAULT_RATE, "rate_limit_per_10s": RATE_LIMIT}
