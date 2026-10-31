# Engineering Assignment: Build Pravar Household Context Service

You are acting as a Senior Backend Engineer completing a take-home assignment for Pravar AI.

Your task is to design, implement, test, and document a production-minded Household Context Service in this repository.

You have access to the local filesystem and terminal. Inspect the actual repository and files before making assumptions. You are expected to own the implementation end-to-end, make sensible engineering decisions, and explain important trade-offs in the documentation.

Do not merely generate code. Build a coherent, testable system that satisfies the contracts and handles the data-quality edge cases described below.

---

# 1. First: inspect the repository

Before writing code:

1. Inspect the complete project structure.
2. Read all files under:
    - `contracts/`
    - `mock_crm/`
    - `custodian_drops/`
3. Read all manifests and CSV files directly from disk.
4. Inspect the existing Git status and commit history.
5. Identify the Python version and available environment.
6. Verify the actual SHA-256 hashes and row counts of the custodian files.
7. Identify any discrepancies between the supplied contracts, manifests, and actual files.

Do not trust copied descriptions when the actual files are available.

Do not modify the mock CRM or source custodian files.

Do not invent missing source data.

If an ambiguity is non-blocking, make a conservative decision and document it. If it materially prevents a correct implementation, ask me before proceeding.

# 2. Assignment requirements

Build a Python 3.11+ service between two independent systems of record and Pravar's AI agents.

The two source systems are:

**Ledgerline CRM**
- Provides households, contacts, relationships, risk profiles, service tiers, advisors, and CRM-to-custodian account associations.
- Exposes a mock REST API.
- Uses bearer authentication.
- Supports cursor pagination and incremental synchronization.
- Can return HTTP 429 and intermittent HTTP 503 errors.
- Supports soft deletes.

**Meridian Custody**
- Provides account, position, and balance data through CSV file drops.
- Each drop contains a manifest.
- Drops can arrive late, out of order, or as corrections to earlier snapshots.

The service must:
- Ingest both sources through independent connectors.
- Normalize their data into a canonical model.
- Persist canonical data.
- Handle retries, duplicate deliveries, corrections, and incremental syncs.
- Reconcile CRM and custodian data.
- Enforce field-level data policy.
- Expose a household context API for agents.
- Expose operational endpoints for ops.
- Provide sync observability and structured findings.
- Include targeted tests for important correctness paths.

Use a modular monolith. Do not introduce unnecessary microservices, Kafka, Redis, or distributed infrastructure.

Recommended stack:
- FastAPI
- SQLAlchemy
- Pydantic
- httpx
- SQLite (unless there is a strong reason to use PostgreSQL)
- pytest
- Decimal for monetary values

Keep dependencies and implementation complexity proportionate to the assignment.

# 3. Mandatory design-first workflow

This is important.

Before implementing the application, create `DESIGN.md` (2–4 pages) covering:

1. Architecture and component responsibilities.
2. Canonical domain model and relationships.
3. Connector interface and how a third connector could be added.
4. CRM incremental synchronization and checkpoint semantics.
5. Custodian drop ingestion, correction, and out-of-order delivery semantics.
6. Idempotency and transaction boundaries.
7. Reconciliation rules and severity model.
8. Data-policy enforcement at ingestion, storage, logging, and API egress.
9. API authentication and authorization.
10. Failure handling, observability, and deliberate omissions.

The initial design must be committed to Git before implementation begins.

Use a commit message such as:

`docs: add initial household context service design`

After that, implement in logical stages with meaningful commits.

Do not rewrite history or squash away the design-first commit.

If Git is not initialized, initialize it. Do not commit secrets, virtual environments, generated databases, or unnecessary files.

# 4. Canonical data model

Use Pravar-owned canonical identifiers. Never use source IDs as canonical primary keys.

Design a model containing at least:

- Household
- Person
- Account
- Ownership
- Position
- Valuation
- SourceRecord / SourceMapping
- SyncRun
- SyncCheckpoint
- Finding
- CustodianDrop

Important domain rules:

### Household
An advisor-facing unit with:
- Name
- Primary advisor
- Risk profile
- Service tier
- Lifecycle status

### Person
A human associated with a household.

Multiple CRM records may represent the same human. Do not blindly merge records based on names or heuristic matching.

When identity is uncertain, retain the records separately and create a possible-duplicate finding.

### Account
A custodial account:
- Registration type
- Status
- Base currency
- Open date
- Source account number
- Household association

Each account belongs to exactly one household for aggregation, but can have multiple owners.

### Ownership
A separate person-account relationship.

This is necessary for joint accounts, trustees, and multiple owners.

### Position
A holding in an account for a specific business date.

Include:
- Symbol
- CUSIP where available
- Asset class
- Quantity
- Price
- Market value
- Currency
- As-of date

### Valuation
The custodian's reported account-level valuation:
- Total market value
- Settled cash
- Currency
- As-of date

### General rules
- Monetary values must use exact Decimal arithmetic.
- Never use floating-point numbers for money.
- Currency must be an explicit ISO 4217 code.
- Store business dates separately from ingestion timestamps.
- Preserve source lineage for every canonical record.
- Never silently correct source values.
- Preserve source records and delivery history where appropriate.
- Use transactions to ensure atomic persistence.

# 5. CRM connector requirements

Base URL:

`http://localhost:8081`

Development bearer token:

`pravar-dev-token`

Do not hard-code credentials into application source.

Relevant endpoints:

- `GET /v2/households`
- `GET /v2/contacts`
- `GET /v2/contacts/{id}`

List endpoints support:
- `updated_since`
- `include_deleted`
- `cursor`
- `limit`

Contract behavior:

- Cursor pagination.
- Limit between 1 and 50; default 25.
- `next_cursor` is null at the end.
- Ordering by `(updated_at, id)` ascending.
- Cursors are opaque and valid for one hour.
- `updated_since` is inclusive.
- Timestamps have one-second precision.
- Multiple records may share the same timestamp.
- Soft deletes are represented by `deleted_at`.
- Default list behavior excludes deleted records; `include_deleted=true` includes them.
- A deletion updates `updated_at`.
- HTTP 429 includes `Retry-After`.
- HTTP 503 can occur intermittently.

Implement:

1. Authentication.
2. Cursor pagination.
3. Incremental synchronization.
4. Soft-delete handling.
5. Bounded retries with exponential backoff and Retry-After support.
6. Idempotent upserts.
7. Safe checkpoint advancement.

Important synchronization rule:

A cursor is a page position, not a durable incremental checkpoint.

Do not advance the durable checkpoint until the entire synchronization window has completed successfully.

Because `updated_since` is inclusive and timestamps have second-level precision, use an overlap strategy and idempotent upserts to safely replay records.

Account for timestamp ties. Do not assume that storing only the maximum timestamp is sufficient without an overlap strategy.

If any page fails, the sync must not advance its durable checkpoint. Replaying already processed records must be safe.

Handle tombstones correctly. Do not interpret a missing record from a default non-deleted list as proof of deletion.

# 6. Custodian file ingestion

Inspect the actual directory structure and manifests.

Each custodian drop has:
- A manifest
- `accounts.csv`
- `positions.csv`
- `balances.csv`

The manifest contains:
- `drop_id`
- `custodian`
- `as_of_date`
- `delivered_at`
- `snapshot_type`
- `supersedes`
- File names, SHA-256 hashes, and row counts

Rules:

### Manifest validation
- Verify all SHA-256 hashes before ingestion.
- Verify row counts, excluding headers.
- Validate expected file names and schemas.
- Validate the supersedes relationship.
- Reject an invalid drop atomically.
- Never partially persist a failed drop.
- A repeated drop ID with identical content is a no-op.
- A repeated drop ID with different content is a conflict.

### Snapshot semantics
All drops are FULL snapshots.

The `as_of_date` is the business date of the data.

The `delivered_at` is the time the file was delivered.

These dates are not interchangeable.

A correction may arrive days later and supersede an earlier drop for the same business date.

The effective snapshot must be determined by the valid supersedes chain, not simply by the most recently delivered folder.

Keep the historical drop records. Do not delete earlier versions.

A missing account in a FULL snapshot means the account is absent from that snapshot; it does not automatically mean the account has permanently closed.

### Important supplied data scenarios

The data includes:
- A correction for October 8.
- A separate October 9 snapshot.
- A correction that arrives after the October 9 snapshot.
- Duplicate position rows.
- A malformed market value in the original October 8 position data.
- Currency-formatted market values containing commas and `$`.
- Lowercase ticker symbols.
- Missing CUSIP values.
- Accounts that change status.
- New accounts appearing in later snapshots.
- Cash-only accounts.

The expected effective October 8 drop is the valid correction `MC-20261008-02`, which supersedes `MC-20261008-01`.

The October 9 snapshot is `MC-20261009-01`.

Do not use delivery time alone to decide which snapshot is authoritative.

### Important verification already performed

The actual October 8 account files were checked using PowerShell:

`2026-10-08_v1/accounts.csv`
`2026-10-08_v2/accounts.csv`

Both produced the same SHA-256:

`548043107A11F4817B570CD5367D8FE38F9940EA72512ED471715A3FF46E56B0`

Both contain nine CSV data rows.

An earlier manually pasted representation appeared to show different contents, but the actual files are byte-for-byte identical. Trust the files and manifest, not the inconsistent pasted representation.

Verify all other files independently.

# 7. Reconciliation requirements

Implement reconciliation as an independent component.

Do not mix reconciliation logic into connectors or API route handlers.

At minimum, reconcile:

1. Custodian accounts against CRM account references.
2. CRM contacts against CRM households.
3. Position totals against custodian account balances.
4. Duplicate positions.
5. Account cash positions against settled cash.
6. Account and household lifecycle discrepancies.
7. Possible duplicate people.
8. Missing or unlinked account relationships.

Findings must be structured and contain:
- Code
- Severity
- Message
- Entity type and entity ID
- Source lineage
- Expected and observed values when relevant
- Sync run ID

Define severity rules centrally and document them.

### Specific data scenarios to handle

#### Chen household
CRM contains:
- Robert Chen (C-2001)
- Bob Chen (C-2002)

They share strong identity signals:
- Same household
- Similar names
- Same DOB
- Same tax identifier after normalization
- Same email after normalization
- Same phone after normalization
- Same role

Do not silently merge them.

Retain both records initially and produce a `POSSIBLE_DUPLICATE_PERSON` finding.

Do not log or expose the sensitive matching values.

#### Account-number normalization

CRM references are inconsistently formatted:

- `88124402`
- `8812 4450`
- `8812-4401`

Normalize valid account numbers to:

`NNNN-NNNN`

A suitable process:
- Remove spaces and hyphens.
- Require exactly eight digits.
- Format as `NNNN-NNNN`.

Preserve the original source value for lineage.

Do not turn arbitrary invalid values into valid account numbers.

#### Account associations

Explicit CRM account references are the authoritative basis for confidently associating accounts with a household.

Examples:

- 4401 -> H-1001
- 4402 -> H-1001
- 4410 -> H-1002
- 4411 -> H-1002
- 4420 -> H-1003
- 4430 -> H-1004
- 4440 -> H-1005
- 4450 -> H-1006

Account 4460 has a title resembling Kenji Sato and CRM notes mentioning a second account, but no explicit CRM account reference.

Do not automatically associate 4460 with H-1005.

Produce an unlinked-account finding. It must not contribute to household totals until its relationship is verified.

Account 4499 has no CRM association. Do not invent one.

#### Missing CRM household

Contact C-2010 references household H-9999, which is absent from the household data.

Produce a finding such as `MISSING_CRM_HOUSEHOLD`.

Do not fabricate the household or silently discard the contact.

#### Deleted household

H-1004 is soft-deleted in CRM.

Its contact remains present in the returned contacts data.

Custodian account 4430 is explicitly CLOSED in the October 9 snapshot.

These are distinct lifecycle facts. Preserve both.

For the context API, use a conservative policy for deleted households and document it.

#### Position anomalies

October 8 account 4401 contains duplicate VTI position rows.

Do not silently deduplicate at ingestion.

October 8 account 4402 has an MSFT reported market value of `4165.00` in the original position file, despite quantity 100 and price 416.50. The corrected snapshot reports `41650.00`.

Do not rewrite the source data. Reconcile and report findings against the effective snapshot.

Some market value strings contain `$` and commas. Parse them safely into Decimal.

The lowercase ticker `voo` should be normalized for matching, while preserving the raw source value.

Missing CUSIP is not automatically invalid if a usable symbol is present.

#### Cash and balance handling

The custodian's total market value includes cash and may include pending settlements.

Cash is also represented as a CASH position in the supplied files.

Avoid double-counting cash by adding both the CASH position and `cash_balance`.

Use the custodian's account-level total market value as the authoritative account valuation.

Position sums are for reconciliation, not for silently replacing the custodian's valuation.

Document the reconciliation calculation and tolerance.

# 8. Data policy: mandatory

Read and parse `contracts/data_policy.yaml`.

Do not hard-code principal-specific field rules into API route handlers.

The policy file defines storage, logging, and egress behavior.

Principals:

- `agent`
- `ops`

Scopes:

- Agent: `context:read`
- Ops: `context:read`, `findings:read`, `sync:run`, `sync:read`

Field policies:

### person.tax_id
- Restricted.
- Store keyed hash.
- Never log.
- Never egress to either principal.

Use a keyed HMAC, not an ordinary unsalted hash, for the stored matching token.

### person.date_of_birth
- Confidential.
- Store encrypted.
- Never log.
- Egress only as age band to both principals.

### person.email
- Confidential.
- Store encrypted.
- Never log.
- Agent receives masked email.
- Ops receives plain email.

### person.phone
- Confidential.
- Store encrypted.
- Never log.
- Agent receives nothing.
- Ops receives last four digits.

### person.notes
- Confidential.
- Store encrypted.
- Never log.
- Both principals receive redacted free text.

### account.account_number
- Confidential.
- Store plain.
- Logs may contain only last four digits.
- Both principals receive last four digits.

### account.primary_tax_id
- Never store.
- Never log.
- Never egress.

The policy also defines:
- Age bands.
- Email masking.
- Last-four formatting.
- Free-text redaction patterns.
- Sync run retention: 400 days.
- Raw source payload retention: 30 days.

Raw source payloads must themselves obey the field policies. Do not store prohibited sensitive data in raw payload archives.

Enforce policy at ingestion/storage and egress. Merely hiding a field in the API response is insufficient if the policy says never store it.

Do not leak sensitive data through:
- Application logs
- Exceptions
- Debug output
- Finding messages
- Validation errors
- Test failure output
- API error responses

The YAML should drive policy behavior wherever practical. Adding a new configured field rule should not require rewriting API routes.

# 9. Fixed Agent Context API contract

Implement the following API exactly.

## Authentication

`Authorization: Bearer <api_key>`

Local keys must be configurable through environment variables:

- `AGENT_API_KEY=agent-key-local-001`
- `OPS_API_KEY=ops-key-local-001`

Do not hard-code keys.

Each key maps to a principal defined in `data_policy.yaml`.

An optional `X-Correlation-ID` header must be propagated through logs and echoed in the response.

If absent, generate one.

## Main endpoint

`GET /v1/households/{household_id}/context`

The household ID is the CRM household identifier.

The 200 response must follow the exact structure in `contracts/agent-context-api.md`.

The response contains:

- `household`
- `members`
- `accounts`
- `totals`
- `freshness`
- `data_quality`
- `correlation_id`

Do not invent a different response schema.

All monetary values must be decimal strings.

`totals` must aggregate only accounts the service is confident belong to the household.

`data_quality.status` must be one of:
- `ok`
- `degraded`
- `unreliable`

Define and document the rules for these states.

An agent will refuse to quote numbers when the status is `unreliable`.

The ops principal receives everything the agent receives, plus a `detail` object on each finding.

The agent principal must never receive finding details.

Fields prohibited by policy must be omitted from the response, not returned as null.

### Errors

Return the exact error structure:

`{ "error": { "code": "...", "message": "...", "correlation_id": "..." } }`

Required status mappings:

- 401: `unauthenticated`
- 403: `forbidden`
- 404: `household_not_found`
- 503: `context_unavailable`

Invalid or missing credentials produce 401.

An authenticated agent requesting an ops-only endpoint produces 403.

If no successful sync has ever completed, return 503.

A failed latest sync must not erase previously successful canonical data.

## Operational endpoints

Design operational endpoints yourself.

At minimum:

- `POST /v1/ops/sync/{source}`
- `GET /v1/ops/sync-runs`
- `GET /v1/ops/sync-runs/{run_id}`

Only ops can access them.

Support triggering a CRM or custodian sync.

Persist run status, timestamps, counts, and errors.

Use correlation IDs and structured logs.

Keep the interface simple. Do not introduce a job queue unless genuinely necessary.

# 10. Architecture expectations

Use clear separation of responsibilities.

Suggested structure (adapt if a better structure emerges):

- `app/api/`
- `app/connectors/`
- `app/domain/`
- `app/models/`
- `app/repositories/`
- `app/services/`
- `app/policy/`
- `app/reconciliation/`
- `app/core/`
- `tests/`

Expected components:

1. CRM connector.
2. Custodian file connector.
3. Shared connector contract.
4. Canonical domain model.
5. Persistence/repositories.
6. Policy engine.
7. Reconciliation engine.
8. Sync orchestration service.
9. Context assembly service.
10. FastAPI routes.
11. Authentication and authorization.
12. Structured logging and correlation middleware.

Adding a third source connector should require implementing a small, well-defined interface rather than changing the core application.

Avoid excessive abstractions and unnecessary design patterns.

# 11. Testing requirements

Write targeted tests for correctness, not just happy paths.

At minimum test:

### CRM
- Cursor pagination.
- Multiple pages.
- Inclusive timestamp overlap.
- Duplicate records across incremental syncs.
- Retry after 429 using Retry-After.
- Retry after 503.
- Failure halfway through pagination does not advance checkpoint.
- Soft deletion.
- Replaying a sync is idempotent.

### Custodian
- Valid manifest and files.
- Hash mismatch rejects entire drop.
- Row-count mismatch rejects entire drop.
- Duplicate identical drop is a no-op.
- Same drop ID with different contents is rejected.
- Correction supersedes earlier snapshot.
- Out-of-order delivery does not select the wrong effective snapshot.
- Malformed rows are handled safely.
- Decimal parsing with currency symbols and commas.
- Duplicate position detection.
- Failed ingestion does not partially persist data.

### Reconciliation
- Duplicate person detection without automatic merge.
- Normalized account-number matching.
- Missing household.
- Unlinked account.
- Position/balance discrepancy.
- Cash handling without double-counting.
- Closed account lifecycle.

### Policy
- Tax IDs never appear in stored plaintext.
- Tax IDs never appear in logs or API output.
- DOB is encrypted and only emitted as an age band.
- Email differs by principal.
- Phone differs by principal.
- Notes are redacted.
- Account numbers are masked.
- `primary_tax_id` is never stored.
- Ops finding details are not returned to agents.

### API
- Authentication success and failure.
- Authorization differences.
- Exact response shape.
- Correct error response structure.
- Correlation ID propagation.
- Deleted household behavior.
- No successful sync returns 503.
- Last successful data remains available after a failed sync.

Use temporary databases and mock HTTP responses. Do not rely on the live CRM sandbox for unit tests.

# 12. Documentation and submission deliverables

Create and maintain:

### DESIGN.md
The initial architecture and design document. Commit it before implementation.

### README.md
Include:
- Project overview.
- Architecture.
- Prerequisites.
- Windows PowerShell setup instructions.
- Environment variables.
- How to start the mock CRM.
- How to run the application.
- How to trigger synchronization.
- Example API requests for agent and ops.
- How to run tests.
- Known limitations.

### AI_USAGE.md
Document how AI tools were used, including meaningful AI-assisted design or implementation decisions. Do not fabricate tool usage or claim work was done manually if it was AI-assisted.

### FINDINGS.md
Document:
- Important data-quality findings discovered in the supplied data.
- Reconciliation rules.
- Severity decisions.
- Known assumptions and limitations.

# 13. Engineering quality expectations

- Use type hints.
- Use Pydantic models at API boundaries.
- Use SQLAlchemy for persistence.
- Use Decimal for monetary values.
- Use timezone-aware UTC timestamps.
- Use transactions at appropriate boundaries.
- Keep source data lineage.
- Use structured logs.
- Never log sensitive values.
- Make error handling explicit.
- Avoid broad exception swallowing.
- Avoid giant service classes.
- Avoid duplicated business logic.
- Avoid unnecessary abstractions.
- Avoid silent data correction.
- Avoid hard-coded secrets.
- Keep the implementation readable and explainable in an interview.

Do not add optional features until the required assignment is complete and tested.

# 14. Execution plan

Work through the following stages:

**Stage 1 — Repository inspection**
- Inspect all files.
- Verify data and contracts.
- Identify discrepancies.
- Summarize your findings.

**Stage 2 — Design**
- Create `DESIGN.md`.
- Explain architecture and decisions.
- Commit the initial design before implementation.

**Stage 3 — Foundation**
- Set up application structure.
- Configure dependencies and settings.
- Create canonical schema and database migrations/schema initialization.
- Implement policy infrastructure.

**Stage 4 — Connectors**
- Implement CRM connector.
- Implement custodian file connector.
- Implement shared connector contract.
- Add unit tests.

**Stage 5 — Synchronization**
- Implement idempotent ingestion.
- Implement durable checkpoints.
- Implement correction and out-of-order handling.
- Implement sync-run tracking.

**Stage 6 — Reconciliation**
- Implement reconciliation rules.
- Persist structured findings.
- Add tests for known edge cases.

**Stage 7 — API**
- Implement authentication and authorization.
- Implement context assembly.
- Implement policy-filtered output.
- Implement operational endpoints.
- Match the fixed API contract exactly.

**Stage 8 — Verification**
- Run all tests.
- Run linting/type checks if configured.
- Test the service against the actual local mock CRM and supplied custodian drops.
- Verify the final API responses.
- Check for sensitive-data leakage.
- Fix failures rather than weakening tests.

**Stage 9 — Documentation and final review**
- Complete README.md, AI_USAGE.md, FINDINGS.md.
- Update DESIGN.md if implementation decisions changed.
- Review the complete repository against every assignment requirement.
- Commit the completed work in logical commits.

# 15. How I want you to work

You have autonomy to make implementation decisions.

However:

- Do not skip repository inspection.
- Do not skip the design-first commit.
- Do not invent data or contract behavior.
- Do not silently ignore inconsistencies.
- Do not implement a simplistic happy-path-only solution.
- Do not overengineer.
- Do not claim a test passed unless you actually ran it.
- Do not mark a requirement complete without evidence.
- Do not hide failing tests.
- Do not weaken tests just to make the suite green.

When a design decision has multiple reasonable approaches, choose the simplest approach that preserves correctness and document the trade-off.

Work in stages, but continue autonomously through the stages when the requirements are clear. Ask me only when a genuinely blocking ambiguity or destructive decision requires my input.

At the end, provide:

1. A concise architecture summary.
2. The final project structure.
3. The Git commit history.
4. Tests executed and their actual results.
5. Assignment requirements satisfied.
6. Any incomplete requirements or known limitations.
7. How I can run and demonstrate the service locally.
8. The most important engineering trade-offs I should be ready to defend in an interview.

Your goal is not merely to make the code run. Your goal is to deliver a credible Senior Backend Engineer submission with strong correctness, clear design, and explainable engineering decisions.