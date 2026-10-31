# Ledgerline CRM API v2 (vendor documentation)

Base URL in this exercise: `http://localhost:8081`

## Authentication

Every request must carry `Authorization: Bearer <token>`. Tokens do not expire in the sandbox. Invalid or missing tokens return `401`.

## Rate limits

Each token may make **10 requests per rolling 10 second window**. Excess requests receive `429 Too Many Requests` with a `Retry-After` header in seconds. Clients that ignore `Retry-After` may be throttled for longer in production.

## Availability

The API is served through a shared gateway and returns `503 Service Unavailable` during brief upstream interruptions. Our SLA covers 99.5% monthly availability. Clients should retry idempotent calls.

## Pagination

List endpoints use cursor pagination. Pass `limit` (1 to 50, default 25) and, for subsequent pages, the `cursor` returned in `meta.next_cursor`. When `next_cursor` is `null` you have reached the end.

Results are ordered by `(updated_at, id)` ascending. Cursors are opaque and valid for one hour.

## Incremental retrieval

List endpoints accept `updated_since`, an ISO-8601 UTC timestamp with seconds precision (`2026-10-01T00:00:00Z`). The comparison is **inclusive**: records whose `updated_at` equals the value are returned. `updated_at` has one second resolution and several records may share the same value.

## Deletion

Records are soft deleted. Deleted records are excluded from list responses unless you pass `include_deleted=true`, in which case they are returned with a non-null `deleted_at`. Deleting a record also updates its `updated_at`.

## Endpoints

### `GET /v2/households`

Query: `updated_since`, `include_deleted`, `cursor`, `limit`.

```json
{
  "data": [
    {
      "id": "H-1001",
      "name": "Chen Family",
      "primary_advisor": "Priya Natarajan",
      "risk_profile": "Moderate",
      "service_tier": "Premier",
      "created_at": "2024-03-11T14:02:00Z",
      "updated_at": "2026-10-01T09:15:00Z",
      "deleted_at": null
    }
  ],
  "meta": { "count": 1, "next_cursor": null, "server_time": "2026-10-11T08:00:00Z" }
}
```

### `GET /v2/contacts`

Query: `updated_since`, `include_deleted`, `cursor`, `limit`.

Contact fields: `id`, `household_id`, `first_name`, `last_name`, `email`, `phone`, `date_of_birth`, `tax_id`, `role`, `custodian_account_numbers` (free-form strings entered by advisors), `notes` (free text), `updated_at`, `deleted_at`.

### `GET /v2/contacts/{id}`

Returns `{"data": {...}}` or `404`.

## Known data entry behavior

`custodian_account_numbers` and `notes` are typed by advisors and support staff without validation.

## Sandbox test hooks

These exist only in the sandbox, to help you exercise incremental sync:

- `POST /_admin/contacts/{id}/touch?field=notes&value=...` updates a field and bumps `updated_at`.
- `POST /_admin/contacts/{id}/delete` soft deletes a contact.
- `POST /_admin/reset` restores the seed data.
