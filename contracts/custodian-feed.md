# Meridian Custody · Nightly Position Feed Specification (v4.1)

## Delivery

Meridian delivers one folder per drop to the advisor's SFTP inbox. In this exercise the inbox is `custodian_drops/`. Folder names are informational; **the manifest is authoritative**.

Each drop contains `accounts.csv`, `positions.csv`, `balances.csv` and `manifest.json`.

## Manifest

```json
{
  "drop_id": "MC-20261008-01",
  "custodian": "Meridian Custody",
  "as_of_date": "2026-10-08",
  "delivered_at": "2026-10-09T02:14:00Z",
  "snapshot_type": "FULL",
  "supersedes": null,
  "files": [{ "name": "positions.csv", "sha256": "...", "row_count": 20 }]
}
```

- `snapshot_type` is always `FULL`: each drop is the complete state of every account as of `as_of_date`. An account or position absent from a drop no longer exists as of that date.
- When Meridian corrects a drop it delivers a new drop for the same `as_of_date` with `supersedes` set to the `drop_id` being replaced. Corrections may arrive days later, after drops for later dates.
- Consumers should verify `sha256` and `row_count` (data rows, excluding the header) before processing.

## accounts.csv

| Column | Notes |
| --- | --- |
| `account_number` | Format `NNNN-NNNN`. |
| `account_title` | Registration title, upper case. |
| `registration_type` | `INDIVIDUAL`, `JOINT`, `ROTH_IRA`, `TRADITIONAL_IRA`, `TRUST`. |
| `open_date` | `MM/DD/YYYY`. |
| `status` | `OPEN` or `CLOSED`. |
| `base_currency` | ISO 4217. |
| `primary_tax_id` | Tax ID of the primary owner. |

## positions.csv

| Column | Notes |
| --- | --- |
| `account_number` | |
| `as_of_date` | `YYYY-MM-DD`. |
| `symbol` | Ticker, or CUSIP for individual bonds. `CASH` denotes the cash sweep. |
| `cusip` | May be blank for cash and some legacy records. |
| `description` | |
| `asset_class` | `EQUITY`, `FIXED_INCOME`, `CASH`. |
| `quantity` | Shares for funds and equities. **Face value for individual bonds.** |
| `price` | Per share for funds and equities. **Percent of par for individual bonds.** |
| `market_value` | In `currency`. Legacy formatting with separators may occur. |
| `currency` | ISO 4217. |

## balances.csv

| Column | Notes |
| --- | --- |
| `account_number` | |
| `as_of_date` | |
| `total_market_value` | Custodian's book of record total for the account, including cash and any pending settlements. |
| `cash_balance` | Settled cash. |
