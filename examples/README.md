# Request examples

Each file is a JSON body sent to the service. See `scripts/demo.sh` for a
runnable end-to-end walkthrough using `curl`.

| File | Endpoint | Purpose |
|------|----------|---------|
| `create-engine.json` | `POST /v1/engines` | Engine sized explicitly (width/depth/seed), non-rotating window, exact tracking on |
| `create-engine-epsilon.json` | `POST /v1/engines` | Engine sized from an error target: width=ceil(e/epsilon), depth=ceil(ln(1/delta)); 10s tumbling windows |
| `events.json` | `POST /v1/engines/{id}/events` | Mixed batch: a bare string, weighted events (`count`), and one with an explicit timestamp |
| `merge-placeholder.json` | `POST /v1/engines/{id}/merge` | Shape of a merge payload — normally you paste the output of `GET /v1/engines/{id}/sketch` |

Event object fields:

- `item` (string, required): the observed item
- `count` (positive integer, default 1): repeat the event that many times
- `timestampMillis` (integer, default = request clock time): event time; events
  before the active window start are rejected and reported as `droppedLate`

## Read endpoints

```
GET /v1/engines/{id}/topk?k=5
GET /v1/engines/{id}/items/{url-encoded-item}
GET /v1/engines/{id}/sketch
GET /v1/engines/{id}/windows
GET /v1/engines/{id}/windows/0?sketch=true
GET /healthz
POST /v1/engines/{id}/flush
POST /v1/admin/advance-time   # body {"millis": N}, manual-time mode only
```
