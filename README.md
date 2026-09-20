# CloudGate — multi-tenant access control plane (simulation)

A FastAPI + SQLAlchemy 2.0 + PostgreSQL service that simulates the control
plane of a multi-tenant VPN/zero-trust gateway. It manages tenants, access
points, IPv4 address pools and devices, and hands out unique IP leases with
heartbeats, generations, revocation and expiry — **without** establishing a
real VPN or touching host routes.

## Concurrency & correctness guarantees

- **Address allocation.** Network and broadcast addresses are never assigned;
  per-pool reserved addresses are excluded too. Allocation, capacity and the
  uniqueness rules run under `SELECT … FOR UPDATE` locks (device → access
  point order) with partial unique indexes as the hard backstop:
  - one `active` lease per device;
  - one `active` lease per `(access_point, ip_address)`;
  - one lease per `(device, idempotency_key)`.
- **Idempotent connect.** A device with an active lease always gets that same
  lease back, with or without an idempotency key. Concurrent connects can
  neither double-allocate IPs nor exceed capacity.
- **Generations.** Every (re)connect bumps the device's generation. Heartbeats
  and disconnects carry a signed session token with the generation; stale
  generations get `409` and can never touch a newer session.
- **Heartbeats / expiry.** Heartbeats are expected every 60 s; a lease is dead
  10 min after its last heartbeat. An expired lease is never revived by a late
  heartbeat — it is closed as `expired` and its address becomes reusable.
- **Revocation.** Revoking a device closes its active lease (`revoked`), and
  the old device token can never reconnect. Revoke racing reconnect leaves no
  active lease behind.
- **Exactly-once release.** Closing is a state transition guarded by a row
  lock; expiry sweep, disconnect and revoke all funnel through the same
  `close_lease`, so every lease records exactly one termination (reason +
  timestamp in `lease_events`).
- **Persistence & recovery.** Leases live in PostgreSQL. Healthy leases
  survive restarts; at startup, leases that expired while the service was down
  are reaped once. A background sweeper then runs continuously.
- **Tenant isolation.** Tenant admin keys only ever see their own rows; other
  tenants' resources answer `404`/`401` rather than leaking existence. Device
  tokens can never drive admin endpoints (`403`).

## API overview

All routes are under `/api/v1`.

| Auth | Method & path | Purpose |
|---|---|---|
| super key | `POST /tenants` | create tenant (admin key returned once) |
| super key | `GET /tenants` | list tenants |
| admin key | `POST /tenants/{id}/pools` | create IPv4 pool + reserved addresses |
| admin key | `GET /tenants/{id}/pools` | pools with `total_hosts`/`usable` |
| admin key | `POST /tenants/{id}/access-points` | create AP (`pool_id`, `capacity`) |
| admin key | `GET /tenants/{id}/access-points` | APs with live `active_leases` |
| admin key | `POST /tenants/{id}/devices` | register device (token returned once) |
| admin key | `GET /tenants/{id}/devices` | list devices |
| admin key | `POST /tenants/{id}/devices/{did}/revoke` | revoke (idempotent) |
| admin key | `GET /tenants/{id}/leases` | lease audit trail (optional `?status=`) |
| device token | `POST /sessions` | connect `{access_point_id, idempotency_key?}` |
| device + session token | `POST /sessions/heartbeat` | heartbeat |
| device + session token | `POST /sessions/disconnect` | graceful disconnect |

Headers: `X-Admin-Key` for admin calls; `X-Device-Token` (or
`Authorization: Bearer …`) for device calls; `X-Session-Token` (or
`Authorization: Bearer …`) for heartbeat/disconnect.

## Run with Docker

```bash
sudo docker compose up --build
# API:  http://127.0.0.1:18099   (docs at /docs)
# DB:   127.0.0.1:15499
```

The app container waits for PostgreSQL, runs `alembic upgrade head`, then
starts uvicorn.

Run the demo against the running stack:

```bash
python3 demo.py
```

Run the test suite inside the compose network (one-off container):

```bash
sudo docker compose run --rm --no-deps \
  -e CLOUDGATE_DATABASE_URL=postgresql+psycopg2://cloudgate:cloudgate@db:5432/cloudgate_test \
  -e CLOUDGATE_SUPER_ADMIN_KEY=test-super-key \
  --entrypoint sh app -c \
  'psql "postgresql://cloudgate:cloudgate@db:5432/cloudgate_test" -c "CREATE DATABASE cloudgate_test" 2>/dev/null; alembic upgrade head; python -m pytest -q'
```

(or simpler: `createdb` once in the db container, then `alembic upgrade head &&
pytest` in the app container).

## Local development

```bash
pip install -r requirements-dev.txt
createdb -h 127.0.0.1 -U cloudgate cloudgate_t12a   # any fresh PG database
export CLOUDGATE_DATABASE_URL=postgresql+psycopg2://cloudgate:cloudgate@127.0.0.1:5432/cloudgate_t12a
alembic upgrade head
python -m pytest -q
uvicorn app.main:app --reload
```

## Configuration (all prefixed `CLOUDGATE_`)

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | local `cloudgate_dev` | SQLAlchemy URL |
| `SUPER_ADMIN_KEY` | `super-admin-key` | bootstrap super-admin key |
| `TOKEN_SECRET` | dev placeholder | HMAC secret for device/session tokens |
| `HEARTBEAT_TTL_SECONDS` | `600` | lease lifetime since last heartbeat |
| `HEARTBEAT_INTERVAL_SECONDS` | `60` | advertised heartbeat cadence |
| `SWEEPER_INTERVAL_SECONDS` | `15` | background expiry sweep interval |
| `RUN_SWEEPER` | `true` | disable for tests that drive sweeps manually |

## Layout

```
app/
  main.py         FastAPI app, startup recovery + sweeper lifecycle
  models.py       ORM models incl. partial unique indexes
  services.py     connect / heartbeat / disconnect / revoke / sweep (locking)
  allocation.py   IPv4 host math (network/broadcast/reserved)
  tokens.py       HMAC device & session tokens
  deps.py         auth dependencies
  routers/        admin & device HTTP routes
alembic/          schema migration
tests/            address exhaustion, capacity race, late heartbeat,
                  revoke race, cross-tenant isolation, restart recovery
demo.py           end-to-end walk-through
```
