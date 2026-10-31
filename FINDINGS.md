# Data Quality Findings

This document describes the data anomalies discovered in the supplied source data, the reconciliation rules implemented to detect them, severity decisions, and known assumptions.

---

## 1. Supplied Data Anomalies

### 1.1 Duplicate VTI Position Rows (Account 4401, Oct-8)

Both `2026-10-08_v1/positions.csv` and `2026-10-08_v2/positions.csv` contain two identical VTI rows for account 4401 (rows 3 and 4). This is preserved as source data. The reconciliation engine emits `DUPLICATE_POSITION` (HIGH severity). The custodian's reported account balance is treated as authoritative; the sum-of-positions is used only for reconciliation, not silently substituted.

### 1.2 MSFT Market Value Off by 10× (Account 4402, Oct-8 v1)

In `2026-10-08_v1/positions.csv`, account 4402 MSFT shows quantity=100, price=416.50, but market_value=4165.00 (should be 41650.00). The corrected drop `MC-20261008-02` reports 41650.00. The effective snapshot is the correction. The v1 data is retained for lineage but not used for household totals.

### 1.3 Currency-Formatted Market Values

Several position rows use currency-formatted values (`"36,200.00"`, `"$38,980.00"`). The custodian connector's `_parse_decimal()` strips `$` and `,` before converting to `Decimal`. The raw string is preserved in `symbol_raw` (N/A for market values — all monetary values are stored as exact `Decimal` strings after parsing).

### 1.4 Lowercase Ticker Symbol (Account 4450)

Account 4450 has symbol `voo` (lowercase) in both Oct-8 drops. The connector normalises to uppercase (`VOO`) and stores the raw value in `symbol_raw` for lineage. No CUSIP is present; this is acceptable when a usable symbol exists.

### 1.5 Chen Household — Possible Duplicate Persons

CRM contacts C-2001 (Robert Chen) and C-2002 (Bob Chen):
- Same household (H-1001)
- Same DOB (after decryption)
- Same normalized tax ID → same HMAC hash
- Similar name (Bob is a common nickname for Robert)
- Same email and phone after normalization

**Decision**: Both records are retained. No auto-merge is performed. The reconciliation engine emits `POSSIBLE_DUPLICATE_PERSON` (MEDIUM severity). Resolution requires a human decision.

**Policy**: The matching values (tax ID hash, DOB, email, phone) are never included in finding messages or API responses.

### 1.6 SSN in Free-Text Notes (Contact C-2003)

Notes field: `"Read back her SSN 987-65-4320 to verify; acct 8812-4402."` The policy engine applies `redact_free_text()` to notes before encrypting at ingestion time. The stored encrypted value contains `[REDACTED:us_tax_id]` and `[REDACTED:account_number]` in place of the sensitive values. The raw SSN is never stored, logged, or returned.

### 1.7 Contact References Missing Household (C-2010 → H-9999)

Contact C-2010 references `household_id = H-9999`, which is absent from the CRM household endpoint. The CRM connector sets `household_id = None` for C-2010. The reconciliation engine emits `MISSING_CRM_HOUSEHOLD` (HIGH severity). No household is fabricated.

### 1.8 Soft-Deleted Household H-1004

H-1004 has `deleted_at` set in the CRM. Contact C-2007 remains present (the CRM does not delete the contact when the household is soft-deleted). Account 4430 is explicitly CLOSED in the Oct-9 custodian snapshot.

**Context API behavior**: `GET /v1/households/H-1004/context` returns HTTP 200 with `household.status = "deleted"` and an empty accounts list. The agent is expected to avoid quoting numbers for a deleted household (the `data_quality.status` will be `degraded` due to lifecycle findings).

### 1.9 Account Number Formatting Inconsistencies

CRM contacts use inconsistent formats for `custodian_account_numbers`:
- `88124402` — no separators
- `8812 4450` — space separator
- `8812-4401` — canonical hyphen

All are normalised to `NNNN-NNNN` by stripping spaces and hyphens, requiring exactly 8 digits. Invalid strings (fewer or more digits) are dropped and logged. The raw source value is preserved for lineage.

### 1.10 Account 4460 — No Explicit CRM Association

Account 4460 appears in the Oct-9 custodian snapshot with title resembling Kenji Sato (H-1005). Contact C-2008 notes mention "Opening a second account" but `custodian_account_numbers` does not list 4460.

**Decision**: 4460 is NOT auto-linked to H-1005. Circumstantial evidence is not sufficient. The reconciliation engine emits `UNLINKED_ACCOUNT` (MEDIUM severity). It does not contribute to H-1005 totals until verified.

### 1.11 Account 4499 — No CRM Association

Account 4499 (J DOE) has no CRM household reference in any drop. It emits `UNLINKED_ACCOUNT` (MEDIUM severity).

### 1.12 Account 4430 Status Change

4430 is OPEN in the Oct-8 snapshot and CLOSED in the Oct-9 snapshot. Both states are preserved in the canonical store (the account's `status` column is updated to CLOSED after the Oct-9 drop is ingested). The lifecycle change is factual, not a finding.

### 1.13 `account.primary_tax_id` in Custodian CSVs

All `accounts.csv` files include a `primary_tax_id` column with plain-text SSNs. The custodian connector reads and immediately discards this value. It is never assigned to the `Account` ORM model, never flushed to the database, and never logged.

### 1.14 Oct-8 v1 vs v2 — Identical Account Files

The SHA-256 hash of `2026-10-08_v1/accounts.csv` and `2026-10-08_v2/accounts.csv` are identical (`548043107A...`). The correction only modifies `positions.csv`. Both drops correctly pass account-file hash validation.

---

## 2. Reconciliation Rules

| Code | Severity | Trigger | Notes |
|------|----------|---------|-------|
| `POSSIBLE_DUPLICATE_PERSON` | MEDIUM | Two persons in same household share ≥2 identity signals (tax_id hash match + name similarity) | No auto-merge; matching values never exposed |
| `MISSING_CRM_HOUSEHOLD` | HIGH | Person's `household_id` not found in canonical households | Contact retained with `household_id = NULL` |
| `UNLINKED_ACCOUNT` | MEDIUM | Custodian account has no CRM household association | Excluded from household totals |
| `DELETED_HOUSEHOLD_OPEN_ACCOUNT` | MEDIUM | Soft-deleted CRM household has an OPEN custodian account | Lifecycle discrepancy; informational |
| `DUPLICATE_POSITION` | HIGH | Same (account, as_of_date, symbol, cusip) appears more than once in an effective drop | Does not auto-deduplicate |
| `POSITION_SUM_MISMATCH` | HIGH (>$1), LOW (<$1) | Sum of non-CASH position market values + cash_balance differs from custodian's `total_market_value` by >$0.05 | Custodian total is always authoritative |

---

## 3. Data Quality Status Rules

The `data_quality.status` field in the context response is computed from findings touching the household:

| Status | Condition |
|--------|-----------|
| `unreliable` | Any `critical` severity finding |
| `degraded` | Any `high` severity finding (no critical) |
| `ok` | Only `medium`/`low` findings, or none |

An agent runtime is expected to refuse to quote numbers when `status = unreliable`.

---

## 4. Severity Rationale

- **HIGH** for `MISSING_CRM_HOUSEHOLD`: a person in limbo is a material data gap that affects household completeness.
- **HIGH** for `DUPLICATE_POSITION`: inflated position sums directly affect household totals (and therefore AUM computations).
- **HIGH** for `POSITION_SUM_MISMATCH > $1`: material discrepancy between positions and the custodian's reported total.
- **MEDIUM** for `POSSIBLE_DUPLICATE_PERSON`: does not directly inflate totals; needs human resolution.
- **MEDIUM** for `UNLINKED_ACCOUNT`: account is excluded from totals, so totals remain correct; the gap is informational.
- **MEDIUM** for `DELETED_HOUSEHOLD_OPEN_ACCOUNT`: lifecycle inconsistency; no immediate financial impact.

---

## 5. Assumptions and Known Limitations

1. **Cash double-counting**: The reconciliation sum uses `non-CASH positions + cash_balance`. This is documented in `app/reconciliation/rules.py`. The `CASH` asset class is the discriminator; asset classes must be consistently typed in the source data.
2. **Effective snapshot selection**: Determined by the supersedes chain in the manifest, not by delivery folder order or timestamp. `MC-20261008-02` supersedes `MC-20261008-01` regardless of which folder is processed first.
3. **Incremental CRM sync overlap**: The checkpoint is the max `updated_at` seen in the prior run. The next run uses `updated_since = checkpoint` (inclusive). Records at the exact boundary are re-fetched and idempotently upserted (no data loss on ties).
4. **Reconciliation runs post-sync only**: There is no scheduled reconciliation run. If source data is corrected without a new sync, findings will not refresh until the next sync.
5. **Tax ID matching uses stored HMAC only**: Matching is reliable only within a single HMAC key. Key rotation requires a re-hashing migration.
