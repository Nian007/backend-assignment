# Agent Context API · v1 (fixed contract)

This is the contract Pravar's agent runtime is built against. Implement it as written. If you believe it is wrong, implement it anyway and argue the change in `DESIGN.md`.

## Authentication

`Authorization: Bearer <api_key>`. Each key maps to a principal defined in `data_policy.yaml`. For local development, configure these two keys through environment variables (do not hard-code them):

| Principal | Local key |
| --- | --- |
| `agent` | `agent-key-local-001` |
| `ops` | `ops-key-local-001` |

An optional `X-Correlation-ID` request header must be propagated through logs and echoed in the response. If absent, generate one.

## `GET /v1/households/{household_id}/context`

`household_id` is the CRM household identifier (for example `H-1001`).

### 200 response

```json
{
  "household": {
    "id": "H-1001",
    "name": "Chen Family",
    "primary_advisor": "Priya Natarajan",
    "risk_profile": "Moderate",
    "service_tier": "Premier",
    "status": "active"
  },
  "members": [
    {
      "person_id": "per_7f3a...",
      "display_name": "Robert Chen",
      "role": "Primary",
      "email": "r***@example-mail.com",
      "age_band": "55-64"
    }
  ],
  "accounts": [
    {
      "account_id": "acc_91bc...",
      "account_number": "****4401",
      "registration_type": "INDIVIDUAL",
      "status": "OPEN",
      "owners": ["per_7f3a..."],
      "as_of_date": "2026-10-09",
      "market_value": { "amount": "159878.75", "currency": "USD" },
      "positions": [
        {
          "symbol": "VTI",
          "asset_class": "EQUITY",
          "quantity": "400",
          "price": "294.02",
          "market_value": { "amount": "117608.00", "currency": "USD" }
        }
      ],
      "lineage": [
        { "source": "meridian_custody", "record_ref": "MC-20261009-01/positions.csv", "ingested_at": "2026-10-11T08:02:11Z" }
      ]
    }
  ],
  "totals": {
    "market_value": { "amount": "0.00", "currency": "USD" },
    "by_asset_class": { "EQUITY": "0.00", "FIXED_INCOME": "0.00", "CASH": "0.00" }
  },
  "freshness": [
    { "source": "ledgerline_crm", "last_successful_sync_at": "2026-10-11T08:00:04Z" },
    { "source": "meridian_custody", "as_of_date": "2026-10-09", "drop_id": "MC-20261009-01" }
  ],
  "data_quality": {
    "status": "ok",
    "findings": [
      { "code": "RECON_BREAK", "severity": "high", "message": "Positions do not sum to custodian balance", "entity": "acc_91bc..." }
    ]
  },
  "correlation_id": "c0a8012e-..."
}
```

Field notes:

- All amounts are decimal strings. `totals` aggregates only accounts the service is confident belong to the household.
- `data_quality.status` is one of `ok`, `degraded`, `unreliable`. You decide and document the rules that produce each. An agent will refuse to quote numbers to a client when the status is `unreliable`.
- The `ops` principal receives everything the `agent` principal receives, plus a `detail` object on each finding with the evidence (expected and observed values, source references). The `agent` principal never receives `detail`.
- Fields not permitted for a principal by `data_policy.yaml` are omitted, not nulled.

### Errors

```json
{ "error": { "code": "household_not_found", "message": "...", "correlation_id": "..." } }
```

| Status | `code` |
| --- | --- |
| 401 | `unauthenticated` |
| 403 | `forbidden` |
| 404 | `household_not_found` |
| 503 | `context_unavailable` (for example, no successful sync has ever completed) |

## Operational endpoints

Design these yourself. At minimum, the `ops` principal must be able to trigger a sync for a source and read the record of past sync runs. The `agent` principal must not.
