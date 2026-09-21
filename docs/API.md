# Meridian TravelOps — Contract & Settlement API

Base URL: `http://localhost:8081`. All bodies are JSON. Errors are
`{"error": {"code": "...", "message": "..."}}` with an appropriate HTTP status
(404 unknown resource, 409 state conflict, 422 validation failure).

Amounts are **decimal strings** in major units (`"1234.56"`); they are stored
as fixed-point minor units. Currencies use ISO-4217 exponents (USD/EUR: 2,
JPY: 0, BHD: 3).

Every mutating endpoint accepts `operator` (string) — recorded with a
timestamp on the resulting rows/events. These records are an operational
audit trail only; the system makes **no legal certification or
non-repudiation claims** about signatures.

## Templates & contracts

### `POST /api/templates`
```json
{"name": "standard", "body": "Contract {{contract_code}} for {{supplier_name}}."}
```
→ `201` template.

### `POST /api/contracts`
```json
{"code": "CTR-1", "template_id": 1, "title": "...", "variables": {"contract_code": "CTR-1", "supplier_name": "Acme"}, "operator": "ops"}
```
Creates the contract plus version 1 (status `draft`). Missing template
variables → `422`. → `201` contract with versions.

### `GET /api/contracts/{id}`
Contract with all versions, signers and the event log.

### `POST /api/contracts/{id}/versions`
```json
{"variables": {...}, "operator": "editor"}
```
Content change ⇒ new version. The previous draft/signing version becomes
`superseded` and its confirmations are void (they were bound to the old
content hash). Only allowed while the contract is `draft`/`signing` →
otherwise `409`.

### `POST /api/contracts/{id}/versions/{v}/initiate-sign`
```json
{"signers": [{"name": "Alice", "role": "ops"}, {"name": "Bob", "role": "finance"}], "operator": "ops"}
```
Freezes the full rendered content and its sha256 digest, attaches the ordered
signer list (seq 1..n), opens a **72-hour** signing window.

### `POST /api/contracts/{id}/versions/{v}/confirm`
```json
{"seq": 1, "operator": "alice"}
```
Confirms one signer, strictly in seq order, bound to this exact version.
Responses include `idempotent: true` when the call is a retry of an
already-applied confirmation (no duplicate state, no duplicate event).
`409` when out of order, expired, withdrawn or superseded.

### `POST /api/contracts/{id}/withdraw`
Only before the first confirmation → `409 already_signed` afterwards.

### `POST /api/contracts/{id}/expire`
Sweeps the active signing version to `expired` if its 72h window has closed
(expiry is also applied lazily on confirm/withdraw attempts).

## Settlement line imports

### `POST /api/imports`
```json
{"source": "supplier-file-2026-09-20", "operator": "ops",
 "lines": [
   {"external_ref": "TXN-1", "contract_code": "CTR-1", "currency": "USD",
    "business_date": "2026-09-20", "amount": "100.25", "description": "rooms"}
 ]}
```
- Whole batch is atomic: any invalid line or conflict rolls back everything
  (no batch row is left behind).
- `external_ref` is the dedup key. Identical re-import → counted in
  `duplicate_count`. Same ref with different content → `409
  external_ref_conflict`.
- Lines for a date already closed → `409 date_closed`.

### `GET /api/imports/{id}` — batch with its lines.

## Allocation rules (versioned)

### `POST /api/allocation-rules`
```json
{"name": "cost-split", "operator": "finops",
 "allocations": [{"target": "ops", "weight": 60}, {"target": "finance", "weight": 30}, {"target": "support", "weight": 10}],
 "remainder_target": "ops"}
```
Each call creates a new immutable version of the named rule. Shares are
`floor(total × weight / Σweights)`; the rounding remainder is assigned in
full to `remainder_target` (default: first target), so allocated amounts
always sum exactly to the original amount.

## Settlements

### `POST /api/settlements`
```json
{"contract_id": 1, "contract_version_id": 3, "currency": "USD",
 "business_date": "2026-09-20", "rule_version_id": 2, "operator": "finops"}
```
Requires a **fully signed** contract version (`409 contract_not_signed`
otherwise); the settlement is permanently bound to that version. Sums the
active settlement lines for (contract, currency, business date) and allocates
per the rule version. Repeating the same
(version, currency, date, rule) request → `409 settlement_exists`.

### `GET /api/settlements/{id}` — settlement with allocation rows
(`is_remainder_sink` marks the row that owns the rounding remainder).

## Daily close & corrections

### `POST /api/closes`
```json
{"date": "2026-09-20", "operator": "finops"}
```
Idempotent. After close, imports/settlements for that date are rejected.
Close and import are serialised by a per-date named lock — a day is either
fully open or fully closed, never half-closed.

### `POST /api/adjustments`
```json
{"target_type": "line", "target_id": 12, "type": "reversal",
 "business_date": "2026-09-21", "reason": "wrong amount", "operator": "finops"}
```
Corrections for closed days never mutate the original record.
`type: "reversal"` posts the exact negation of the original amount;
`type: "adjustment"` requires an explicit `amount`. `business_date` must be a
later, still-open day (`422`/`409` otherwise).
