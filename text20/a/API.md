# SignalBoard HTTP API

Base URL: `http://<host>:8080`
All request/response bodies are JSON (`Content-Type: application/json`).
All monetary values are **integer cents** (`BIGINT`, e.g. `1399` = $13.99).
All timestamps are RFC 3339 with an explicit UTC offset (`...Z` or `...+08:00`).

Error responses have a uniform shape:

```json
{ "error": { "code": "version_conflict", "message": "human readable" } }
```

## Authentication

Two independent credential types:

| Caller | Header | Scope |
|---|---|---|
| Management / POS ingest | `Authorization: Bearer <ADMIN_TOKEN>` | All admin endpoints |
| Physical screen | `X-Screen-Token: <screen-token>` (or `Authorization: Screen <token>`) | Only the one store the screen belongs to |

Screens never specify a store id; the token fully determines which store a
screen may read. There is no way for a screen token to address another store.

- `401 unauthorized` — missing/invalid token.
- Screens calling admin endpoints, or admin tokens calling `/v1/screen/*`,
  are rejected.

---

## Health

### `GET /healthz`
Returns `{"status":"ok"}` or `503` if the database is unreachable.

---

## Stores

### `POST /v1/admin/stores`
```json
{ "name": "Downtown Bistro", "timezone": "America/New_York" }
```
`timezone` is an IANA zone. It governs the local calendar day used for sales
bucketing, sold-out recovery, and screen menu `local_day`.

### `GET /v1/admin/stores` / `GET /v1/admin/stores/{storeID}`
Store object includes the current `menu_version` (starts at `0`).

---

## Dishes (the editable draft / catalog)

Editing dishes never changes what live screens show; changes appear only after
a publish.

### `POST /v1/admin/stores/{storeID}/dishes`
```json
{
  "sku": "BK-001",
  "name": "Classic Cheeseburger",
  "base_price": 999,
  "active": true,
  "daily_limit": 20,
  "sort_order": 1
}
```
- `daily_limit`: positive integer, or omit/`null` for unlimited. When the
  day's sold quantity reaches the limit the item is marked sold-out for that
  local day.
- Unique `(store_id, sku)` → `409 sku_conflict`.

### `GET /v1/admin/stores/{storeID}/dishes`
### `PUT /v1/admin/stores/{storeID}/dishes/{dishID}`
PUT takes the same fields as create (`active` defaults to its prior value when
omitted).
### `DELETE /v1/admin/stores/{storeID}/dishes/{dishID}` → `204`

### `POST /v1/admin/stores/{storeID}/dishes/import`
Batch upsert keyed on `(store_id, sku)`. **At most 500 items.**

```json
{ "items": [
  { "sku": "BK-001", "name": "Classic Cheeseburger", "base_price": 999,
    "active": true, "daily_limit": 20, "sort_order": 1 }
]}
```

- The whole batch is validated first (invalid field, duplicate SKU inside the
  batch, size > 500 → `400`, nothing written).
- It then runs in a **single transaction**: any failure rolls back **every**
  row (all-or-nothing). On success returns `{"imported": N, "items": [...]}`.

---

## Publishing (immutable versions, optimistic concurrency)

### `POST /v1/admin/stores/{storeID}/publish`
```json
{
  "expected_version": 1,
  "note": "spring menu",
  "temporary_prices": [
    { "sku": "DR-001", "price": 249,
      "start_at": "2026-03-01T17:00:00-05:00",
      "end_at":   "2026-03-01T19:00:00-05:00" }
  ]
}
```

- `expected_version` is **required** and must equal the store's current
  `menu_version`. The store row is locked `FOR UPDATE` for the transaction, so
  two concurrent publishes carrying the same expected version serialize: the
  first commits and bumps the version, the second gets
  `409 version_conflict`. Exactly one succeeds.
- On success creates version `current + 1` containing an **immutable
  snapshot** of all active dishes plus the supplied temporary prices, and
  bumps the store's `menu_version`.
- A `temporary_prices` entry referencing a SKU not active in the publish →
  `400 unknown_sku`.

**Temporary price windows**
- Half-open `[start_at, end_at)` (the price applies at `start_at`, not at
  `end_at`). Adjacent windows `[a,b)` + `[b,c)` are allowed; true overlaps are
  rejected (`400 invalid_temporary_price` within one request; a GiST exclusion
  constraint is the final guard against concurrent writes, surfacing as
  `409 overlapping_temporary_price`).
- They travel with the publish. The screen picks the in-effect price purely by
  evaluating the current instant against the windows — **no republish is
  needed** for an automatic switch.

### `GET /v1/admin/stores/{storeID}/versions`
### `GET /v1/admin/stores/{storeID}/versions/{version}`
Returns the immutable version with `items` and `temporary_prices`.

---

## Sales events (idempotent ingest, auto sell-out)

### `POST /v1/admin/stores/{storeID}/sales-events`
```json
{ "event_id": "POS-77821", "sku": "BK-001", "qty": 2,
  "occurred_at": "2026-03-01T18:32:05-05:00" }
```

- **Idempotency key = `(store_id, event_id)`** (unique index; insert uses
  `ON CONFLICT DO NOTHING`).
  - First submission: `201`, counted once.
  - Identical replay (same `sku`, `qty`, `occurred_at`): `200` with
    `"duplicate": true`; the daily counter is **not** changed.
  - Same `event_id`, different content: `409 event_id_conflict`.
- Counters are upserted atomically (`qty = qty + EXCLUDED.qty`); concurrent
  events serialize on the counter row, so increments are never lost or
  double-counted.
- **Day bucketing** uses `occurred_at` converted to the **store's timezone**.
  A late event that occurred yesterday but arrives today counts for
  yesterday (and cannot disturb today's sold-out state).
- When the day's quantity reaches the SKU's `daily_limit`, the item is marked
  sold-out for that local day; the response includes `"sold_out": true`.
- Sold-out is day-scoped, so it **recovers automatically** on the next local
  day with no action.

### `GET /v1/admin/stores/{storeID}/daily-sales?date=YYYY-MM-DD`
Returns `{"date", "sales":[{sku,qty}], "sold_outs":[{sku,triggered_at}]}`.
`date` defaults to today in the store timezone.

---

## Screens

### `POST /v1/admin/stores/{storeID}/screens`
```json
{ "name": "Front Counter Display" }
```
Returns `201` with a freshly generated opaque `token` **exactly once**; only
its SHA-256 hash is stored. Use that token as `X-Screen-Token`.

### `GET /v1/admin/stores/{storeID}/screens`
Lists screens with derived `online` state.

### `GET /v1/screen/menu`  *(screen token)*
The complete, currently effective menu. Always the **full** latest menu (this
is also the reconnect path — no deltas).

```json
{
  "store_id": 7,
  "menu_version": 3,
  "effective_at": "2026-03-01T22:14:03.12Z",
  "local_day": "2026-03-01",
  "items": [
    { "sku": "DR-001", "name": "Fresh Lemonade",
      "base_price": 349, "display_price": 249,
      "on_temporary_price": true, "sold_out": false,
      "display_order": 4 }
  ]
}
```

- Read inside one read-only repeatable-read transaction → a single snapshot,
  so a response can never mix an old version with new prices or a fresh
  sold-out flag with stale items.
- `display_price` is the temporary price when a window is in effect, else
  `base_price`.
- `sold_out` reflects today's day-scoped marker.

**Caching / conditional requests**

- Every response carries a strong `ETag` and `Cache-Control: no-cache`.
- The ETag is a digest of exactly what changes rendering: published version,
  local day, the set of temporary prices active **at the evaluated instant**,
  and the sold-out set. Therefore:
  - unchanged repeat poll with `If-None-Match: <etag>` → `304 Not Modified`;
  - a publish, a temporary-price window crossing its boundary (no republish),
    a sold-out transition, or a cross-day recovery all change the ETag →
    stale token gets a full `200`; a draft edit that is not republished does
    **not** change it.
  - `If-Match` is supported; mismatch → `412 Precondition Failed`.

### `POST /v1/screen/heartbeat` *(screen token)*
Marks the screen alive and returns `{online, menu_version, ...}`. A screen
with no heartbeat in the last `SCREEN_OFFLINE_AFTER` (default **90s**) is
considered offline; on reconnect it heartbeats and fetches the full latest
menu via `GET /v1/screen/menu`.

### `GET /v1/screen/status` *(screen token)*
`{screen_id, online, last_heartbeat, offline_threshold_seconds, menu_version}`.

---

## Status codes used

`200` `201` `204` `304` `400` `401` `404` `409` `412` `500` `503`
