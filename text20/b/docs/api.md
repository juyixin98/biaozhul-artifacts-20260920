# SignalBoard API

Digital menu-board publishing &amp; synchronization for restaurant chains.
All money amounts are **integer cents**. All timestamps are RFC 3339 with
explicit UTC offset; the service stores and compares them in UTC.

- Base URL: `http://localhost:8080`
- Content type: `application/json`
- Auth:
  - Management API — `Authorization: Bearer <MANAGEMENT_API_KEY>`
  - Screen API — `Authorization: Bearer <screen token>` (provisioned per screen)
- Error body: `{"error": "message"}`

## Status codes

| Code | Meaning in this API |
|------|---------------------|
| 200/201/202/204 | Success |
| 304 | `If-None-Match` matched the current state |
| 400 | Validation error (bad body, overlapping windows, batch > 500, …) |
| 401 | Missing/incorrect API key or screen token |
| 404 | Store/dish/menu not found |
| 409 | Idempotency conflict (same `event_id`, different content), or DB-level overlap |
| 412 | Optimistic version mismatch (`expected_version` stale), or failed `If-Match` |

---

## Management API

### Stores

| Method | Path | Description |
|--------|------|-------------|
| POST | `/v1/stores` | Create a store |
| GET | `/v1/stores` | List stores |
| GET | `/v1/stores/{storeID}` | Get a store |

```bash
POST /v1/stores
{"name": "Signal Burger NYC", "timezone": "America/New_York"}
```
`timezone` is an IANA zone; daily sales and sellout are bucketed in it.

### Dishes

| Method | Path | Description |
|--------|------|-------------|
| POST | `/v1/stores/{storeID}/dishes` | Create dish `{"name","base_price"}` |
| GET | `/v1/stores/{storeID}/dishes` | List dishes |
| PUT | `/v1/stores/{storeID}/dishes/{dishID}/price` | `{"price": 1099}` |
| PUT | `/v1/stores/{storeID}/dishes/{dishID}/active` | `{"is_active": false}` |
| PUT | `/v1/stores/{storeID}/dishes/{dishID}/threshold` | Daily sellout threshold `{"threshold": 50}` |

### Drafts &amp; batch import

The draft never affects live screens until it is published.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/v1/stores/{storeID}/draft` | Read the draft |
| PUT | `/v1/stores/{storeID}/draft` | Replace draft contents (atomic) |
| POST | `/v1/stores/{storeID}/import` | Batch import (same contract as PUT draft) |

```json
{"items": [{"dish_id": 1}, {"dish_id": 2, "price": 999}]}
```

- `price` is optional; omit it to follow the dish's catalog `base_price`.
- At most **500** items. If **any** item is invalid (unknown/inactive dish,
  negative price, duplicate line) the **whole batch rolls back**; the prior
  draft stays intact.

### Publishing

```
POST /v1/stores/{storeID}/publish
{
  "expected_version": 0,
  "published_by": "alice",
  "temp_prices": [
    {"dish_id": 3, "price": 299,
     "starts_at": "2026-09-20T11:00:00-04:00",
     "ends_at":   "2026-09-20T14:00:00-04:00"}
  ]
}
```

- `expected_version` is the published version the caller last saw (`0` for
  the first publish). On success a new immutable version (previous + 1) is
  snapshotted. If the current version differs, the response is **412**;
  concurrent publishers carrying the same `expected_version` serialize, so
  exactly one succeeds and the others get 412.
- `temp_prices` windows are **half-open `[starts_at, ends_at)`** and must not
  overlap per dish. Adjacent windows (`[a,b)`+`[b,c)`) are allowed. The
  database enforces this with a GiST exclusion constraint, so even
  concurrent writers cannot create an overlap.
- Temporary prices activate and deactivate automatically at their boundaries
  — screens see the new price with **no republish** needed.

| Method | Path | Description |
|--------|------|-------------|
| POST | `/v1/stores/{storeID}/publish` | Snapshot the draft into an immutable version |
| GET | `/v1/stores/{storeID}/menu?version=N` | Fetch a published version (latest if omitted) |
| PUT | `/v1/stores/{storeID}/temp-prices` | Replace temp-price schedule of the **latest** version |

Older versions are immutable; only the newest version's schedule is edited.

### Sales events

```
POST /v1/stores/{storeID}/sales
{"event_id": "pos-2026-09-20-0001",
 "dish_id": 2, "quantity": 3,
 "occurred_at": "2026-09-20T12:34:56-04:00"}
```

- Response `202` with `{"day_total","sold_out","threshold","sales_day"}`.
- `(store_id, event_id)` is the idempotency key:
  - identical replay → counted once, response carries `"duplicate": true`;
  - same id with different `dish_id`/`quantity`/`occurred_at` → **409**.
- The event is attributed to the **store-local calendar date** of
  `occurred_at` (late POS events still land on the day they happened).
- When the day's quantity reaches the dish threshold, the dish is marked
  **sold out** for that day; a new store-local day restores availability
  automatically.

### Screens (provisioning)

| Method | Path | Description |
|--------|------|-------------|
| POST | `/v1/stores/{storeID}/screens` | Register a screen; **the token is returned once** |
| GET | `/v1/stores/{storeID}/screens` | List screens with live `online` flag |

---

## Screen API (screen token only)

There is deliberately no store id in these paths: the token pins every
response to one store, so a screen cannot read another store's menu.

### `GET /screen/v1/menu`

Returns the complete current menu in one consistent snapshot (version +
effective prices + sellout flags — never a mix of states):

```json
{
  "store_id": 1, "version": 2,
  "published_at": "2026-09-20T10:38:00Z",
  "generated_at": "2026-09-20T10:40:00Z",
  "lines": [
    {"dish_id": 1, "name": "Classic Burger", "price": 999, "sold_out": false},
    {"dish_id": 2, "name": "Cheeseburger", "price": 1099, "sold_out": true}
  ]
}
```

Conditional caching:

- Every response carries a strong `ETag` over the state (published version +
  every active temp price line + every sellout flag). After **any** publish,
  price activation/expiry or sellout change the ETag changes, so a stale
  cache can never be served.
- `If-None-Match: <etag>` → **304** when nothing changed (empty body).
- `If-Match: <etag>` → **412** when state has moved on.
- `HEAD` is supported.

### `POST /screen/v1/heartbeat`

Marks the screen alive. Screens that have not sent a heartbeat in **90
seconds** show `"online": false` in the management list. On reconnect the
client re-pulls `GET /screen/v1/menu` (always the full latest snapshot); the
heartbeat response includes `"reconnect": true` when the prior gap exceeded
the threshold.

---

## End-to-end example

```bash
# 1. store + dishes
curl -s -X POST localhost:8080/v1/stores -H "Authorization: Bearer $K" \
  -d '{"name":"NYC","timezone":"America/New_York"}'
curl -s -X POST localhost:8080/v1/stores/1/dishes -H "Authorization: Bearer $K" \
  -d '{"name":"Burger","base_price":999}'

# 2. draft -> publish
curl -s -X PUT localhost:8080/v1/stores/1/draft -H "Authorization: Bearer $K" \
  -d '{"items":[{"dish_id":1}]}'
curl -s -X POST localhost:8080/v1/stores/1/publish -H "Authorization: Bearer $K" \
  -d '{"expected_version":0}'

# 3. register a screen, save its token
curl -s -X POST localhost:8080/v1/stores/1/screens -H "Authorization: Bearer $K" \
  -d '{"name":"counter-1"}'

# 4. screen pulls menu, polls with its token (then sends heartbeats)
curl -s localhost:8080/screen/v1/menu -H "Authorization: Bearer $SCREEN_TOKEN" -D -
```
