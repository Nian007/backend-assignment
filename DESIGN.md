# Design · Household Context Service

> First draft written before implementation. Revisions noted inline where implementation decisions changed.

---

## 1. Problem Framing and Assumptions

### What is being built

A backend service that sits between two messy systems of record and Pravar's AI agents. It ingests household and contact data from Ledgerline CRM (a REST API) and account/position/balance data from Meridian Custody (nightly CSV file drops), reconciles them into a clean canonical model, enforces the firm's data policy, and serves a trusted household context to agents and operations engineers.

The core tension: both sources are correct within their own domain, but they disagree on identity, account numbering, and timing. The service must never silently hide that disagreement. Its job is to surface it honestly while still giving agents something they can act on.

### Assumptions

| Area | Assumption |
|------|------------|
| Storage | SQLite for local development. The schema and repository layer are designed so switching to PostgreSQL requires only a connection string change. |
| Concurrency | Single-writer model. Syncs are triggered one at a time. No distributed locking needed. |
| Sync execution | Synchronous (blocking HTTP calls). At the scale of this assignment, async adds complexity without benefit. |
| Tenant model | Single firm. Multi-tenancy is not in scope. |
| Corrections | A corrected custodian drop (`supersedes != null`) is authoritative for its `as_of_date`. The superseded drop is retained for history. |
| Duplicate persons | Retain both records and raise a finding. Never auto-merge. A wrong merge in wealth management produces wrong financial advice. |
| Deleted households | Return a 200 response with `status: deleted` and empty accounts. The household is a known canonical entity; 404 would mislead the caller. |
| Encryption key management | Fernet symmetric encryption for PII fields. Keys are read from environment variables at startup. The service refuses to start without them. No key rotation is implemented (out of scope). |
| Account number format | Canonical form is `NNNN-NNNN`. Normalization: strip spaces and hyphens, require exactly 8 digits, format with hyphen at position 4. |

### What the brief left ambiguous (resolved)

- **Effective snapshot selection**: determined by the `supersedes` chain in the manifest, not by folder name or delivery timestamp.
- **Duplicate person threshold**: two or more matching identity signals after normalization triggers the finding. Exact matching logic is documented in §6.
- **Data quality status for deleted households**: `degraded` (the household is known but is in a terminal state).
- **`account.primary_tax_id` in custodian CSVs**: read, immediately discarded, never stored or logged. The policy says `store: never`.

---

## 2. Architecture

### Component diagram

```
 ┌─────────────────────────────────────────────────────────────────┐
 │                         External Sources                        │
 │                                                                 │
 │   CRM API (mock)                  Custodian File Drops          │
 │   localhost:8081                  custodian_drops/              │
 └────────────┬──────────────────────────────┬────────────────────┘
              │                              │
              ▼                              ▼
 ┌────────────────────────────────────────────────────────────────┐
 │                        app/connectors/                         │
 │                                                                │
 │     CRMConnector                CustodianConnector             │
 │     (implements ConnectorBase)  (implements ConnectorBase)     │
 └──────────────────────────┬─────────────────────────────────────┘
                            │
                            ▼
 ┌─────────────────────────────────────────────────────────────────┐
 │                     app/services/SyncOrchestrator               │
 │  Creates SyncRun record · calls connector.run() · saves result  │
 └──────────────────────────┬──────────────────────────────────────┘
                            │
                            ▼
 ┌─────────────────────────────────────────────────────────────────┐
 │                   app/policy/ (store layer)                     │
 │  Enforces store: rules at ingestion time                        │
 │  · keyed-hash tax_id  · encrypt PII  · discard primary_tax_id  │
 └──────────────────────────┬──────────────────────────────────────┘
                            │
                            ▼
 ┌─────────────────────────────────────────────────────────────────┐
 │              Canonical Store  (SQLite / SQLAlchemy)             │
 │  households · persons · accounts · ownerships · positions       │
 │  valuations · custodian_drops · sync_runs · findings            │
 └──────┬─────────────────────────────────────┬────────────────────┘
        │                                     │
        ▼                                     ▼
 ┌─────────────────┐                 ┌────────────────────────────┐
 │  Reconciliation │                 │  ContextAssembler          │
 │  Engine         │                 │  (app/services/)           │
 │  (runs after    │                 │  Reads canonical store,    │
 │   each sync)    │                 │  applies policy egress     │
 │  Writes findings│                 │  transforms per principal  │
 └─────────────────┘                 └──────────┬─────────────────┘
                                                │
                                                ▼
                                     ┌────────────────────────────┐
                                     │  app/api/  (FastAPI)       │
                                     │  Auth middleware            │
                                     │  GET /v1/households/…      │
                                     │  POST /v1/ops/sync/…       │
                                     │  GET /v1/ops/sync-runs     │
                                     └────────────────────────────┘
```

### Module responsibilities

| Module | Responsibility |
|--------|---------------|
| `app/core/` | Settings (pydantic-settings, env), structlog configuration, DB session factory, retry utilities |
| `app/connectors/` | `ConnectorBase` protocol; `CRMConnector`; `CustodianConnector` |
| `app/models/` | SQLAlchemy ORM table definitions |
| `app/domain/` | Pydantic models used at API boundaries and across internal service calls |
| `app/repositories/` | Data access — one class per aggregate root; no SQL in service layer |
| `app/services/` | `SyncOrchestrator` (runs connectors, saves run records); `ContextAssembler` (assembles API response) |
| `app/reconciliation/` | `ReconciliationEngine`; one function per rule; writes `Finding` records |
| `app/policy/` | `PolicyEngine` (loads `data_policy.yaml`); store-time transforms; egress transforms; logging scrubber |
| `app/api/` | FastAPI routers, auth dependency, correlation middleware |
| `tests/` | pytest unit and integration tests |

---

## 3. Canonical Model

### Identity

Pravar owns all canonical identifiers. Source identifiers are stored as attributes for lineage.

- `hh_<uuid4>` — household
- `per_<uuid4>` — person
- `acc_<uuid4>` — account

CRM `household_id` (`H-1001`) and custodian account numbers (`8812-4401`) are foreign keys into the source systems, not canonical keys.

Account number normalization: `8812-4401`, `88124402`, `8812 4450` all normalize to `NNNN-NNNN` by stripping non-digits, asserting exactly 8 digits, then inserting the hyphen. Invalid strings (wrong length, non-numeric) produce an `INVALID_ACCOUNT_REF` finding.

### Tables

```
households
  id              TEXT PK  (hh_<uuid>)
  source_id       TEXT     (CRM household_id, e.g. H-1001)
  name            TEXT
  primary_advisor TEXT
  risk_profile    TEXT
  service_tier    TEXT
  status          TEXT     (active | deleted)
  ingested_at     DATETIME UTC
  updated_at      DATETIME UTC
  deleted_at      DATETIME UTC nullable

persons
  id              TEXT PK  (per_<uuid>)
  household_id    TEXT FK → households.id (nullable: contact may reference unknown household)
  source_id       TEXT     (CRM contact_id, e.g. C-2001)
  first_name      TEXT
  last_name       TEXT
  role            TEXT
  tax_id_hash     TEXT     (keyed HMAC-SHA256; never the raw value)
  email_enc       BLOB     (Fernet-encrypted)
  phone_enc       BLOB     (Fernet-encrypted)
  dob_enc         BLOB     (Fernet-encrypted)
  notes_enc       BLOB     (Fernet-encrypted; redact_free_text applied before encrypt)
  ingested_at     DATETIME UTC
  updated_at      DATETIME UTC
  deleted_at      DATETIME UTC nullable

accounts
  id              TEXT PK  (acc_<uuid>)
  household_id    TEXT FK → households.id nullable (null = unlinked)
  account_number  TEXT     (canonical NNNN-NNNN form)
  registration_type TEXT
  status          TEXT     (OPEN | CLOSED)
  base_currency   TEXT     (ISO 4217)
  open_date       DATE
  source_id       TEXT     (custodian account_number as delivered)
  ingested_at     DATETIME UTC
  updated_at      DATETIME UTC
  NOTE: primary_tax_id from custodian CSV is NEVER stored.

ownerships
  id              TEXT PK
  person_id       TEXT FK → persons.id
  account_id      TEXT FK → accounts.id
  role            TEXT     (owner | joint_owner | trustee | beneficiary)
  UNIQUE(person_id, account_id)

custodian_drops
  id              TEXT PK
  drop_id         TEXT UNIQUE  (e.g. MC-20261008-01)
  custodian       TEXT
  as_of_date      DATE
  delivered_at    DATETIME UTC
  snapshot_type   TEXT     (always FULL)
  supersedes      TEXT nullable (drop_id of superseded drop)
  status          TEXT     (pending | ingested | superseded | rejected)
  ingested_at     DATETIME UTC

positions
  id              TEXT PK
  account_id      TEXT FK → accounts.id
  drop_id         TEXT FK → custodian_drops.id
  as_of_date      DATE
  symbol          TEXT     (uppercased for matching; raw value in symbol_raw)
  symbol_raw      TEXT     (original value from file)
  cusip           TEXT nullable
  description     TEXT
  asset_class     TEXT     (EQUITY | FIXED_INCOME | CASH)
  quantity        TEXT     (stored as exact string; Decimal in Python)
  price           TEXT     (stored as exact string; Decimal in Python)
  market_value    TEXT     (stored as exact string; Decimal in Python)
  currency        TEXT
  ingested_at     DATETIME UTC

valuations
  id              TEXT PK
  account_id      TEXT FK → accounts.id
  drop_id         TEXT FK → custodian_drops.id
  as_of_date      DATE
  total_market_value TEXT  (exact Decimal string)
  cash_balance       TEXT  (exact Decimal string)
  currency           TEXT
  ingested_at        DATETIME UTC

sync_runs
  id               TEXT PK  (uuid)
  source           TEXT     (ledgerline_crm | meridian_custody)
  run_type         TEXT     (full | incremental)
  status           TEXT     (running | success | failed)
  started_at       DATETIME UTC
  completed_at     DATETIME UTC nullable
  records_ingested INTEGER
  records_skipped  INTEGER
  errors_count     INTEGER
  checkpoint_at    TEXT nullable  (new checkpoint value after success)
  error_detail     TEXT nullable  (JSON; first N errors)

sync_checkpoints
  id               TEXT PK
  source           TEXT UNIQUE
  last_successful_at TEXT   (ISO-8601; CRM: max updated_at; custodian: latest as_of_date)
  updated_at       DATETIME UTC

findings
  id               TEXT PK  (uuid)
  code             TEXT     (e.g. DUPLICATE_POSITION)
  severity         TEXT     (low | medium | high | critical)
  message          TEXT
  entity_type      TEXT     (household | person | account | position)
  entity_id        TEXT     (canonical ID)
  source           TEXT     (ledgerline_crm | meridian_custody | reconciliation)
  drop_id          TEXT nullable
  sync_run_id      TEXT FK → sync_runs.id
  expected_value   TEXT nullable
  observed_value   TEXT nullable
  created_at       DATETIME UTC
```

### Business time vs. system time

- `as_of_date`: the business date of the data (from the custodian manifest or position row). Never inferred from delivery time.
- `ingested_at`, `updated_at`: system timestamps, always UTC, always timezone-aware.
- Both are stored separately on every canonical record. Never conflated.

### Monetary values

All monetary amounts are stored as exact decimal strings (e.g. `"159878.75"`), read and computed as Python `Decimal`, and returned as decimal strings in API responses. No floats anywhere in the pipeline.

---

## 4. Connector Contract

Every source connector implements this protocol:

```python
from typing import Protocol, ClassVar
from dataclasses import dataclass, field

@dataclass
class SyncResult:
    source: str
    run_id: str
    records_ingested: int = 0
    records_skipped: int = 0
    errors: list[str] = field(default_factory=list)
    new_checkpoint: str | None = None  # None = do not advance checkpoint

class ConnectorBase(Protocol):
    source_name: ClassVar[str]

    async def run(
        self,
        run_id: str,
        db: Session,
        checkpoint: str | None,   # None on first run
    ) -> SyncResult: ...
```

What the platform provides to every connector at construction:
- Structured logger (bound with `source`, `run_id`)
- DB session
- `retry_with_backoff(fn, max_attempts, base_delay)` utility
- `PolicyEngine` instance (for store-time PII handling)
- `settings` (base URLs, tokens, drop directory path)

What a new connector must provide:
- `source_name` class constant (used as the key in `CONNECTORS` registry)
- `run()` implementation
- Its own retry logic using the provided utility

**Adding a third source** requires: (1) create `app/connectors/new_source.py` implementing `ConnectorBase`, (2) add `"new_source": NewConnector` to the `CONNECTORS` registry in `app/connectors/__init__.py`, (3) add any new config fields to `Settings`. Zero changes to the orchestrator, reconciliation engine, or API routes.

---

## 5. Sync Semantics

### CRM (Ledgerline)

**First run (no checkpoint):** full sync — both `/v2/households` and `/v2/contacts` without `updated_since`, with `include_deleted=true`.

**Incremental run:** `updated_since = last_checkpoint_at` (the exact value, not minus one second). The CRM API uses inclusive comparison, so records at the boundary are re-fetched. Idempotent upserts make replaying them safe.

**Checkpoint advancement:** only after all pages of both households and contacts succeed. The new checkpoint is `max(updated_at)` across all records seen in the run. If any page fails, the checkpoint stays at the previous value and the next run replays from there.

**Why this is safe:** `updated_since` is inclusive, so the overlap at the boundary is intentional. The same record appearing twice in consecutive runs is fine because we upsert on `source_id`. The only risk is a record updated between the start of the sync and the checkpoint write — acceptable given this is a single-writer system.

**Soft deletes:** always fetch with `include_deleted=true`. A contact with `deleted_at != null` maps to a canonical `person` with `deleted_at` set. We never infer deletion from absence in a non-deleted list (the API only excludes deleted records by default, not permanently).

**Rate limits / faults:**
- `429 Too Many Requests` → respect `Retry-After` header, then retry
- `503 Service Unavailable` → exponential backoff: 0.5s, 1s, 2s, 4s, 8s (5 attempts max)
- Any non-retriable error on any page → mark run `failed`, do not advance checkpoint

### Custodian (Meridian)

**Drop discovery:** scan `custodian_drops/` for subdirectories containing `manifest.json`. The directory name is informational — the manifest is authoritative.

**Manifest validation (before touching any CSV):**
1. Verify all file names match the manifest.
2. Compute SHA-256 of each file, compare to manifest. Reject entire drop on mismatch.
3. Count data rows (excluding header), compare to manifest `row_count`. Reject on mismatch.
4. Validate `supersedes` references a known drop ID (or is null).
5. Any failure → mark drop `rejected`, write a `MANIFEST_VALIDATION_FAILED` finding, return without persisting anything.

**Idempotency:**
- Same `drop_id` + same file hashes → no-op (already ingested). Log and skip.
- Same `drop_id` + different hashes → `CONFLICT_DROP` finding, reject. The original ingested data is preserved.

**Effective snapshot per `as_of_date`:** the drop for that date that no other drop supersedes. Concretely: among all ingested drops for a given `as_of_date`, follow `supersedes` pointers; the terminal node (superseded by nobody) is effective. A drop is processed into canonical positions/valuations only if it is effective.

**Out-of-order delivery:** drops are processed in `as_of_date` order (ascending), not `delivered_at` order. A correction arriving after a later date's drop correctly supersedes its predecessor because effective snapshot determination is recalculated on each sync run, not locked at ingestion time.

**`account.primary_tax_id`:** parsed from the CSV row into a local variable, then immediately discarded. It is never passed to an ORM model, never assigned to a field, never logged.

**Transaction boundary:** each drop is processed in a single database transaction. On any error, the transaction rolls back and no partial data is persisted.

---

## 6. Reconciliation and Data Quality

The `ReconciliationEngine` runs after every sync. It reads from the canonical store and writes structured `Finding` records. It has no write path to canonical entity tables — reconciliation does not silently correct data.

### Rules

| Code | Severity | Check |
|------|----------|-------|
| `DUPLICATE_POSITION` | high | Same `(account_id, as_of_date, symbol, cusip)` in a single drop appears more than once |
| `POSITION_SUM_MISMATCH` | high | Sum of position `market_value` for the effective snapshot (excluding duplicates after the first) differs from `valuation.total_market_value` by more than $0.05 |
| `CASH_POSITION_MATCHES_BALANCE` | info | Verify CASH position market_value equals `cash_balance`; mismatches are `medium` |
| `POSSIBLE_DUPLICATE_PERSON` | medium | Two persons in the same household match on ≥2 of: same `tax_id_hash`, same normalized+lowercased email (post-decrypt), same normalized phone (digits only), same `dob` |
| `MISSING_CRM_HOUSEHOLD` | high | A contact's `household_id` has no corresponding household in the canonical store |
| `UNLINKED_ACCOUNT` | medium | A custodian account exists with no CRM contact listing it in `custodian_account_numbers` (after normalization) |
| `DELETED_HOUSEHOLD_OPEN_ACCOUNT` | medium | A soft-deleted household has an associated account with `status = OPEN` |
| `ACCOUNT_STATUS_TRANSITION` | low | An account's status changed between the previous effective snapshot and the current one |

**Duplicate person detection:** to compare without decrypting PII unnecessarily, tax_id comparison uses the stored hash directly (matching hashes → same tax_id). Email and phone comparison requires decryption; this is done in memory only, result is not logged. The finding message identifies entity IDs only, never the matching values.

**Position sum reconciliation for account 4401 (Oct-8):** the duplicate VTI row makes the raw sum exceed the balance. The engine detects the duplicate, raises `DUPLICATE_POSITION`, and computes the balance check using the sum excluding the duplicated row. This correctly reflects the custodian's total.

**Data quality status (per household):**

```
unreliable: no successful sync has ever completed
            OR any critical finding on an account in this household

degraded:   any high finding on an account or member in this household
            OR the household itself is deleted

ok:         only medium or low findings, or no findings
```

`ok` does not mean the data is perfect — it means it is trustworthy enough for the agent to quote.

### Finding structure

```json
{
  "id": "uuid",
  "code": "DUPLICATE_POSITION",
  "severity": "high",
  "message": "Account acc_... has duplicate VTI position for 2026-10-08 in drop MC-20261008-01",
  "entity_type": "position",
  "entity_id": "pos_...",
  "source": "meridian_custody",
  "drop_id": "MC-20261008-01",
  "sync_run_id": "run_uuid",
  "expected_value": null,
  "observed_value": "2 rows for (8812-4401, 2026-10-08, VTI, 922908769)",
  "created_at": "2026-10-11T08:02:00Z"
}
```

The `ops` principal receives a `detail` object (expected + observed values). The `agent` principal receives only `code`, `severity`, `message`, and `entity`. This distinction is enforced in `ContextAssembler`, not in the route handler.

---

## 7. Security and Data Policy

The `PolicyEngine` loads `contracts/data_policy.yaml` at startup. Field rules are driven entirely by the YAML. Route handlers never contain field-specific logic.

### Three enforcement points

**1. At ingestion (store-time):**

| Field | Treatment |
|-------|-----------|
| `person.tax_id` | `hmac.new(HMAC_SECRET_KEY.encode(), normalize(tax_id).encode(), sha256).hexdigest()`. The normalized raw value is immediately deleted from local scope. |
| `person.date_of_birth` | `Fernet(DATA_ENCRYPTION_KEY).encrypt(dob.isoformat().encode())` |
| `person.email` | Fernet-encrypted after lowercasing and stripping whitespace (normalization needed for duplicate detection) |
| `person.phone` | Fernet-encrypted |
| `person.notes` | `redact_free_text` patterns applied first, then Fernet-encrypted. This means SSNs and account numbers embedded in notes are redacted before storage. |
| `account.primary_tax_id` | Read from CSV row, used in no operation, immediately discarded. Never assigned to any variable that survives the row parser function. |

**2. At logging:**

The structlog pipeline includes a `PiiScrubber` processor. Any log record containing keys tagged `log: deny` in the policy YAML has those values replaced with `[REDACTED]`. Account numbers in log records use the `last4` transform (`****4401`). Exception tracebacks are caught before logging and stripped of any values that came from a PII field. The scrubber list is derived from the YAML at startup, so adding a new `log: deny` field requires no code change.

**3. At API egress:**

`ContextAssembler` calls `PolicyEngine.apply_egress(field_path, value, principal)` for every field subject to a transform. The return value is either the transformed value or `OMIT`. Fields returning `OMIT` are excluded from the response dict entirely — they are not present as null. The transform registry is:

| Transform name | Behavior |
|----------------|----------|
| `deny` | Return `OMIT` |
| `plain` | Return value as-is |
| `age_band` | Decrypt DOB, compute age, return band string (e.g. `"55-64"`) |
| `mask_email` | Decrypt email, return `r***@domain.com` |
| `last4` | Return `****NNNN` |
| `redact_free_text` | Apply regex patterns from policy YAML, replace matches with `[REDACTED:{pattern}]` |

New principals, new fields, or new transforms require YAML edits only.

**Key management assumptions:** `DATA_ENCRYPTION_KEY` is a Fernet key (URL-safe base64, 32 bytes). `HMAC_SECRET_KEY` is a 32-byte hex string. Both are mandatory env vars — the service raises a configuration error at startup if either is missing. Key rotation is not implemented; the DESIGN notes this as a known limitation.

---

## 8. Observability

### Sync run records

Every run creates a `sync_runs` row before the connector starts. Status progresses: `running` → `success` | `failed`. The row captures `records_ingested`, `records_skipped`, `errors_count`, and (on failure) the first N error messages as JSON. These records are queryable via the `GET /v1/ops/sync-runs` endpoint.

Retention: `sync_run_records_days: 400` per policy. A cleanup job is not implemented in this submission (noted as a known omission).

### Structured logging

`structlog` with a JSON renderer in production. Every log record carries:
- `correlation_id` (from request context, or generated for background sync runs)
- `run_id` (during sync runs)
- `source` (connector name)
- `event` (message)
- `level`
- `timestamp` (UTC ISO-8601)

The `PiiScrubber` processor runs before the renderer.

### Request middleware

`CorrelationMiddleware` reads `X-Correlation-ID` from incoming requests (or generates a UUID4 if absent), stores it in a `contextvars.ContextVar`, and adds it to all log records for the duration of the request. The correlation ID is echoed in every API response, including error responses.

### Alerting (what I would watch at 3am)

- Any `sync_runs.status = failed` — sync broke
- Any `sync_runs.completed_at IS NULL` for a run started more than 30 minutes ago — sync hung
- Any `findings.code = POSITION_SUM_MISMATCH` with `observed_value > $1000` discrepancy
- No successful CRM sync in the last 25 hours (missed a nightly run)

---

## 9. Trade-offs and What I Left Out

| Decision | Trade-off |
|----------|-----------|
| **SQLite** | Easy local dev, no install. Cost: no concurrent writes, no fine-grained row locking. Acceptable because syncs are single-writer and serialized. |
| **Synchronous SQLAlchemy** | Simpler code, easier to reason about transactions. Cost: sync runs block the event loop. Acceptable because syncs are triggered manually and are not latency-sensitive. |
| **No auto-merge of duplicate persons** | Correct. A wrong person merge in wealth management produces wrong aggregations. The `POSSIBLE_DUPLICATE_PERSON` finding gives an operator the signal to manually resolve it. |
| **Inclusive `updated_since` + idempotent upserts** | Handles the timestamp-tie problem without needing an overlap window. Cost: re-processes boundary records on every incremental sync. At this data volume, negligible. |
| **Fernet over asymmetric encryption** | Fernet is simpler and sufficient for field-level encryption where the service needs to decrypt for egress. Cost: no key-splitting (the service that encrypts can also decrypt). Acceptable at this scale. |
| **No job queue** | Sync is triggered via `POST /v1/ops/sync/{source}` and runs synchronously within the request. Simple, easy to observe. Cost: a long sync holds the HTTP connection open. A production system would use a task queue. |
| **Duplicate positions not silently removed** | Source truth is preserved. The reconciliation engine surfaces the duplicate as a `high` finding and the balance check uses the deduplicated sum. Cost: slightly more complex reconciliation logic. |
| **Effective snapshot via supersedes chain** | Correct. Folder name and delivery timestamp are unreliable. Cost: requires reading the manifest before any CSV. |

### Deliberately omitted

- **Notification hook** — optional per brief. Would be a `NotificationBackend` interface with a threshold check post-reconciliation.
- **Prometheus metrics endpoint** — optional per brief.
- **docker-compose** — optional per brief.
- **Crash-safety demo** — optional per brief. The design supports it: a run that dies with `status = running` can be detected and re-started from the last checkpoint.
- **Async connectors** — would benefit a production system with many sources but adds complexity disproportionate to the assignment.
- **Key rotation** — the store layer would need a key ID column on each encrypted row to support rotation. Known limitation.
- **sync_run retention cleanup job** — policy says 400 days; a background sweep is not implemented.

---

## 10. Proposed Changes to Contracts (optional)

None are proposed. The contracts are implemented as written. One observation worth noting: the agent context API does not include a `household.deleted_at` field, which would be useful for the agent to distinguish a deleted household from a household whose data is merely degraded. The current design returns `status: deleted` in the household object, which is sufficient.
