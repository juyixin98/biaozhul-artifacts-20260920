# SignalBoard API

Base URL: `http://localhost:8080`. All bodies are JSON. All timestamps are
RFC-3339 with an explicit UTC offset (e.g. `2026-09-20T12:00:00+08:00`).
Prices are integer cents. `item_key` matches `^[a-z0-9][a-z0-9-]{0,63}$`.

Errors: `{"error": "message"}` with a fitting status (`400` validation,
`401` auth, `404` missing, `409` conflict).

## Admin API

### Stores

| Method | Path | Description |
|---|---|---|
| POST | `/api/stores` | Create store `{name, timezone}` (IANA name) → `201` |
| GET | `/api/stores/{storeID}` | Store incl. `current_version` |
| GET | `/api/stores/{storeID}/menu` | Live menu preview (incl. `sold_today`) |
| GET | `/api/stores/{storeID}/versions` | Published versions, newest first |

### Draft (never affects the live menu)

| Method | Path | Description |
|---|---|---|
| GET | `/api/stores/{storeID}/draft` | Draft items + temp prices |
| PUT | `/api/stores/{storeID}/draft/items/{itemKey}` | Upsert item `{name, price_cents, sold_out_threshold, position}` |
| DELETE | `/api/stores/{storeID}/draft/items/{itemKey}` | Remove item (`404` if absent) |
| POST | `/api/stores/{storeID}/draft/import` | Atomic batch import (below) |
| POST | `/api/stores/{storeID}/draft/temp-prices` | Add temp-price band (below) |
| DELETE | `/api/stores/{storeID}/draft/temp-prices/{id}` | Remove band (`404` if absent) |

**Batch import** — `{items: [...], temp_prices: [...]}`, at most **500
items**. The whole batch runs in one transaction: any invalid entry or
overlapping band rolls everything back (`400`/`409`), nothing is applied
partially.

**Temp prices** — `{item_key, price_cents, starts_at, ends_at}`, half-open
`[starts_at, ends_at)`, `starts_at < ends_at` required. Bands for the same
item may touch (`a.ends_at == b.starts_at`) but never overlap → `409`. The
rule is a database exclusion constraint, so concurrent writers cannot bypass
it. Bands are published together with the menu and take effect automatically
at their start time.

### Publish

```
POST /api/stores/{storeID}/publish
{"expected_version": 0}
→ 201 {"id": "...", "version": 1, "published_at": "..."}
→ 409 {"error": "expected_version does not match the store's current version"}
```

Atomically copies the current draft into a new immutable version. With
concurrent publishes carrying the same `expected_version`, exactly one
succeeds.

### Sales events

```
POST /api/stores/{storeID}/sales-events
{"id": "<uuid>", "item_key": "classic-burger", "quantity": 2,
 "occurred_at": "2026-09-20T12:03:00+08:00"}
→ 201 {"status": "recorded"}    first time
→ 200 {"status": "duplicate"}   same id, same content (not counted again)
→ 409                           same id, different content
```

Quantity accumulates into the store-local day of `occurred_at` (late events
are attributed to the day they happened). When the day's total reaches an
item's `sold_out_threshold` (> 0), the item shows `sold_out: true`; a new
store-local day resets it.

```
GET /api/stores/{storeID}/sales?day=2026-09-20
→ {"day": "...", "sales": [{"item_key": "...", "day": "...", "quantity": 2}, ...]}
```

### Screens

| Method | Path | Description |
|---|---|---|
| POST | `/api/stores/{storeID}/screens` | Create screen `{name}` → `201` incl. one-time `token` |
| GET | `/api/stores/{storeID}/screens` | List screens with `online` flag (heartbeat within 90 s) |

## Screen API

Authenticate with `Authorization: Bearer <token>`. A token belongs to exactly
one screen of one store; screens can only read their own store.

```
GET /screen/menu
→ 200  ETag: "..."
       Cache-Control: no-cache
       {"store_id": "...", "version": 1, "generated_at": "...",
        "items": [{"item_key": "...", "name": "...", "position": 1,
                   "base_price_cents": 1299, "price_cents": 999,
                   "temp_price_active": true, "sold_out": false}, ...]}

GET /screen/menu  with  If-None-Match: "<etag>"
→ 304 (empty body) when nothing visible changed
```

The ETag covers version + visible item state, so it changes on publish,
price/temp-price changes and sold-out flips — a stale cache can never be
served. The body is produced by a single query, so one response never mixes
old and new state. Reconnecting screens always get the complete current menu
here.

```
POST /screen/heartbeat → 204
```

Send at least every 90 s; otherwise the screen is reported `online: false`.
