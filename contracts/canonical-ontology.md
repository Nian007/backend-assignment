# Canonical Wealth Ontology (minimum)

Pravar's agents reason over a small set of canonical entities that are independent of any one vendor. The entities below are the minimum the agent layer depends on. You will need more (for lineage, sync state, findings and so on), and you may restructure storage however you like, provided the meaning of these entities is preserved.

## Household

The unit an advisor serves. A household has a name, a primary advisor, a risk profile, a service tier and a lifecycle status (`active` or `deleted`).

## Person

A human being associated with a household, in a role such as primary, spouse or trustee. A person is a person: two source records describing the same human are one person to the agent, or are explicitly flagged as a suspected duplicate.

## Account

A custodial account. It has a registration type, a status, a base currency and an open date. An account belongs to exactly one household for the purposes of aggregation, and may have several owners.

## Ownership

The relationship between a person and an account, with a role (`owner`, `joint_owner`, `trustee`, ...).

## Position

A holding in an account as of a business date: instrument identifiers, asset class, quantity, price and market value.

## Valuation

The custodian's record of an account's total value and settled cash as of a business date.

## Rules that hold everywhere

1. Monetary amounts are exact decimals with an explicit ISO 4217 currency. No floats.
2. Every canonical record can be traced to the source records and the sync run or file drop that produced it.
3. Business dates (`as_of_date`) and system timestamps (`ingested_at`, `updated_at`) are different things and are stored separately.
4. Canonical identifiers are owned by Pravar. Source identifiers are attributes, never primary keys.
