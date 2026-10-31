# Household Context Service

A production-minded canonical context service that ingests data from the Ledgerline CRM and Meridian Custody, reconciles them, enforces a YAML-driven data policy, and exposes a household context API for Pravar AI agents.

## Architecture

```
Ledgerline CRM API          Meridian Custody (CSV drops)
       │                              │
       ▼                              ▼
  CRM Connector               Custodian Connector
       │                              │
       └──────────┬───────────────────┘
                  ▼
           Sync Orchestrator   ← manages run lifecycle & checkpoint
                  │
         Policy Engine         ← enforces store-time rules
                  │
         Canonical Store       ← SQLite via SQLAlchemy (WAL mode)
                  │
      ┌───────────┼────────────┐
      ▼           ▼            ▼
 Reconciliation  Context    Sync Runs
   Engine       Assembler   & Findings
                  │
           Policy (egress)    ← per-principal transforms
                  │
           FastAPI Routes
                  │
       Agent / Ops principals
```

Key components:
- **`app/connectors/`** — `CRMConnector` (cursor pagination, incremental, retries) and `CustodianConnector` (SHA-256 validation, supersedes chain, atomic drops). Adding a third source = implement `ConnectorBase`, register in `CONNECTOR_FACTORIES`.
- **`app/policy/`** — `PolicyEngine` reads `contracts/data_policy.yaml`; enforces store/log/egress rules without hard-coded field lists in routes.
- **`app/reconciliation/`** — Six independent rule functions run post-sync; findings are persisted atomically.
- **`app/services/context_assembler.py`** — Assembles the household context response and applies egress transforms per principal.
- **`app/ui/index.html`** — Dark-theme ops console served at `/ui/`.

## Prerequisites

- Python 3.11+ (tested on 3.14)
- Node.js (for the mock CRM server)
- `pip install -r requirements.txt`

## Setup

### macOS / Linux

```bash
# 1. Clone the repository
git clone https://github.com/Nian007/backend-assignment.git
cd backend-assignment

# 2. Create and activate virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Generate required encryption keys
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# Copy the output — use it as DATA_ENCRYPTION_KEY below

# 5. Create a .env file (never commit this)
cat > .env <<'EOF'
DATA_ENCRYPTION_KEY=<paste_key_here>
HMAC_SECRET_KEY=<any_32plus_char_random_string>
AGENT_API_KEY=agent-key-local-001
OPS_API_KEY=ops-key-local-001
EOF
```

### Windows PowerShell

```powershell
# 1. Clone the repository
git clone https://github.com/Nian007/backend-assignment.git
cd backend-assignment

# 2. Create and activate virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 3. Install dependencies
pip install -r requirements.txt

# 4. Generate required encryption keys
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# Copy the output — use it as DATA_ENCRYPTION_KEY below

# 5. Create a .env file (never commit this)
@"
DATA_ENCRYPTION_KEY=<paste_key_here>
HMAC_SECRET_KEY=<any_32plus_char_random_string>
AGENT_API_KEY=agent-key-local-001
OPS_API_KEY=ops-key-local-001
"@ | Out-File -Encoding utf8 .env
```

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `DATA_ENCRYPTION_KEY` | Yes | Fernet key (base64) for PII field encryption |
| `HMAC_SECRET_KEY` | Yes | Secret for keyed HMAC tax_id hashing |
| `AGENT_API_KEY` | Yes | Bearer token for the `agent` principal |
| `OPS_API_KEY` | Yes | Bearer token for the `ops` principal |
| `DATABASE_URL` | No | SQLite path (default: `sqlite:///household_context.db`) |
| `CRM_BASE_URL` | No | CRM service URL (default: `http://localhost:8081`) |
| `CRM_TOKEN` | No | CRM bearer token (default: `pravar-dev-token`) |
| `CUSTODIAN_DROPS_DIR` | No | Drop folder path (default: `custodian_drops`) |

## Start the Mock CRM

The mock CRM is a Python Flask server. Start it in a separate terminal before running syncs.

**macOS / Linux**
```bash
cd mock_crm
python3 server.py
# Listens on http://localhost:8081
```

**Windows PowerShell**
```powershell
cd mock_crm
python server.py
# Listens on http://localhost:8081
```

## Run the Service

**macOS / Linux**
```bash
# Export env vars (or use a .env file with python-dotenv)
export DATA_ENCRYPTION_KEY="<your_key>"
export HMAC_SECRET_KEY="<your_hmac_secret>"
export AGENT_API_KEY="agent-key-local-001"
export OPS_API_KEY="ops-key-local-001"

uvicorn app.main:app --host 127.0.0.1 --port 8090 --reload
```

**Windows PowerShell**
```powershell
$env:DATA_ENCRYPTION_KEY = "<your_key>"
$env:HMAC_SECRET_KEY = "<your_hmac_secret>"
$env:AGENT_API_KEY = "agent-key-local-001"
$env:OPS_API_KEY = "ops-key-local-001"

uvicorn app.main:app --host 127.0.0.1 --port 8090 --reload
```

The service starts at `http://127.0.0.1:8090`.  
The demo console is at `http://127.0.0.1:8090/ui/`.  
The architecture deep-dive is at `http://127.0.0.1:8090/architecture`.  
API docs are at `http://127.0.0.1:8090/docs`.

## Trigger Synchronization

**macOS / Linux**
```bash
# Trigger CRM sync
curl -X POST http://127.0.0.1:8090/v1/ops/sync/crm \
  -H "Authorization: Bearer ops-key-local-001"

# Trigger custodian sync
curl -X POST http://127.0.0.1:8090/v1/ops/sync/custodian \
  -H "Authorization: Bearer ops-key-local-001"

# List recent sync runs
curl http://127.0.0.1:8090/v1/ops/sync-runs \
  -H "Authorization: Bearer ops-key-local-001"
```

**Windows PowerShell**
```powershell
# Trigger CRM sync
curl.exe -X POST http://127.0.0.1:8090/v1/ops/sync/crm `
  -H "Authorization: Bearer ops-key-local-001"

# Trigger custodian sync
curl.exe -X POST http://127.0.0.1:8090/v1/ops/sync/custodian `
  -H "Authorization: Bearer ops-key-local-001"

# List recent sync runs
curl.exe http://127.0.0.1:8090/v1/ops/sync-runs `
  -H "Authorization: Bearer ops-key-local-001"
```

## Example API Requests

### Agent — get household context

**macOS / Linux**
```bash
curl http://127.0.0.1:8090/v1/households/H-1001/context \
  -H "Authorization: Bearer agent-key-local-001" \
  -H "X-Correlation-ID: my-trace-001"
```

**Windows PowerShell**
```powershell
curl.exe http://127.0.0.1:8090/v1/households/H-1001/context `
  -H "Authorization: Bearer agent-key-local-001" `
  -H "X-Correlation-ID: my-trace-001"
```

### Ops — get household context (includes finding detail)

**macOS / Linux**
```bash
curl http://127.0.0.1:8090/v1/households/H-1001/context \
  -H "Authorization: Bearer ops-key-local-001"
```

**Windows PowerShell**
```powershell
curl.exe http://127.0.0.1:8090/v1/households/H-1001/context `
  -H "Authorization: Bearer ops-key-local-001"
```

### Ops — get a specific sync run

**macOS / Linux**
```bash
curl http://127.0.0.1:8090/v1/ops/sync-runs/<run_id> \
  -H "Authorization: Bearer ops-key-local-001"
```

**Windows PowerShell**
```powershell
curl.exe http://127.0.0.1:8090/v1/ops/sync-runs/<run_id> `
  -H "Authorization: Bearer ops-key-local-001"
```

### Health check

```bash
curl http://127.0.0.1:8090/health
```

## Run Tests

**macOS / Linux**
```bash
export DATA_ENCRYPTION_KEY=$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
export HMAC_SECRET_KEY="deadbeefdeadbeefdeadbeefdeadbeef"
export AGENT_API_KEY="agent-key-local-001"
export OPS_API_KEY="ops-key-local-001"

python3 -m pytest tests/ -v
```

**Windows PowerShell**
```powershell
$env:DATA_ENCRYPTION_KEY = (python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
$env:HMAC_SECRET_KEY = "deadbeefdeadbeefdeadbeefdeadbeef"
$env:AGENT_API_KEY = "agent-key-local-001"
$env:OPS_API_KEY = "ops-key-local-001"

python -m pytest tests/ -v
```

Expected output: **96 passed**.

Test coverage:
- `tests/test_crm_connector.py` — 9 tests (pagination, retry, checkpoint semantics, soft delete, PII)
- `tests/test_custodian_connector.py` — 15 tests (hash validation, drop correction, out-of-order, atomicity)
- `tests/test_reconciliation.py` — 15 tests (duplicate persons, missing household, unlinked accounts, balance mismatch)
- `tests/test_sync_orchestrator.py` — 17 tests (checkpoint advancement, ops endpoints, auth, correlation ID)
- `tests/test_policy_engine.py` — 21 tests (tax ID hash, DOB age band, email/phone/notes transforms)
- `tests/test_context_api.py` — 19 tests (auth, response shape, principal differences, deleted household)

## Known Limitations

1. **Synchronous sync execution** — `POST /v1/ops/sync/{source}` blocks until complete. For long custodian drops a job queue (e.g. Celery) would be appropriate in production.
2. **SQLite** — Sufficient for local/single-instance use. PostgreSQL recommended for concurrent writers or high query volume.
3. **No automatic reconciliation scheduling** — Reconciliation runs after each sync. A nightly scheduled reconciliation run is not implemented.
4. **Tax ID matching uses HMAC hash only** — Duplicate-person detection compares stored HMAC hashes. Two persons with different HMAC secrets (e.g. after a key rotation) would not match even if the raw values were identical.
5. **Fernet encryption requires key rotation procedure** — Rotating `DATA_ENCRYPTION_KEY` requires re-encrypting stored values. No rotation migration script is provided.
6. **Account 4460 / 4499** — Unlinked accounts. 4460 has circumstantial CRM evidence (notes) but no explicit `custodian_account_numbers` entry. Per design, it is not auto-linked. 4499 has no CRM association at all. Both appear as `UNLINKED_ACCOUNT` findings.
7. **UI** — The ops console at `/ui/` is for demonstration only. It uses plain `fetch()` and requires CORS to be satisfied (same origin in the demo setup).
