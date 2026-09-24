# 多机器人命名隔离命令网关 / Multi-Robot Namespace-Isolated Command Gateway

A pure-backend **ROS 2 multi-robot command gateway** built with **Python +
rclpy + FastAPI**. Each registered robot id maps to its own ROS 2 namespace;
commands are published *only* into the registered namespace, sequences never
roll back, expired commands are dropped with a recorded reason, and a mapping
change revokes every outstanding authorization immediately. Queries and SSE
event subscriptions are per-robot isolated.

No frontend. Nothing about ROS, HMAC, or sequencing is mocked — the automated
tests run real rclpy participants that discover each other over DDS and a
real FastAPI/uvicorn server.

---

## 1. What it guarantees

| Requirement | Where it is enforced |
|---|---|
| Robot id → independent namespace mapping | `registry.py` (single source of truth) |
| Command carries **target, sequence, validity, tester identity** | `models.py`, published in the ROS envelope |
| Gateway publishes **only** into registered namespaces | `registry.fqn()` + `ros_bridge.py` (no arbitrary-topic API) |
| **Absolute topics forbidden** (`/...`) and **cross-namespace escape** (`..`, `~`) rejected | `naming.py` `qualify_topic()` chokepoint |
| Same-target sequence **never rolls back**; duplicates rejected | `registry.accept_sequence()` (per robot+target watermark) |
| **Expired** commands dropped, with reason recorded | expiry check in `app.py`; JSONL audit in `audit.py` |
| **Mapping change immediately invalidates** old authorizations | monotonic `epoch` in `registry.py`; HMAC token binds it in `auth.py` |
| Cross-robot / forged / mismatched-tester requests refused | `auth.py` (real HMAC-SHA256, constant-time compare) |
| Query isolation | `GET /robots/{id}/state` only ever returns that robot |
| Event-subscription isolation + immediate cut-off on remap | `events.py` per-robot bus + `mapping_reset` stream close |
| Robot **reconnection** receives the last command | latched `RELIABLE + TRANSIENT_LOCAL` publishers |
| Two robots with the **same relative topic name** stay separate | the namespace prefix makes `/ns/a/cmd/move` ≠ `/ns/b/cmd/move` |

---

## 2. Repository layout

```
src/mr_gateway/
  config.py          env-driven settings (secrets, TTL, audit path, port)
  naming.py          robot-id / namespace / target validation + qualify_topic()
  auth.py            HMAC-SHA256 token issue/verify (robot + tester + epoch)
  registry.py        robot→namespace map, epochs, per-target sequence watermarks
  audit.py           append-only JSONL audit log (records every drop reason)
  events.py          per-robot SSE event bus; close_robot() on mapping change
  ros_bridge.py      dedicated rclpy thread; latched, namespace-scoped publishers
  models.py          FastAPI/Pydantic request & response models
  app.py             FastAPI app: admin + robot HTTP API and SSE routes
  main.py            uvicorn entry point (mr-gateway)
  synthetic_robot.py a real subscriber node for demos/acceptance (mr-synthetic-robot)
tests/               unit (naming/auth/registry) + full ROS/HTTP integration tests
scripts/acceptance.sh  black-box acceptance over real processes + curl
examples/            sample JSON request bodies
requirements-lock.txt  pinned dependency versions
```

---

## 3. Prerequisites

* **ROS 2 Jazzy** installed (`apt install ros-jazzy-ros-base` is enough) so
  that `rclpy` and `std_msgs` are available. rclpy is **not** a PyPI package.
* Python 3.12 (the one ROS Jazzy uses).
* FastAPI/uvicorn/httpx/pytest — versions pinned in `requirements-lock.txt`
  (already present in the reference environment).

---

## 4. Install (local)

```bash
cd /home/admin/Downloads/biaozhul/P048/a
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-lock.txt
pip install -e .
```

> If you use the system Python (as the reference machine does), there is no
> need for a venv: just export the source path (the commands below do this).

---

## 5. Run locally

Open **four** terminals. Every terminal must source ROS first.

### Terminal A — start the gateway

```bash
cd /home/admin/Downloads/biaozhul/P048/a
source /opt/ros/jazzy/setup.bash
export PYTHONPATH="$PWD/src:${PYTHONPATH:-}"
export ROS_LOCALHOST_ONLY=1
export MRGW_HMAC_SECRET="$(openssl rand -hex 32)"     # real HMAC key
export MRGW_ADMIN_TOKEN="change-me-admin-token"       # guards admin API
mr-gateway          # or: python3 -m mr_gateway.main
# -> uvicorn on http://0.0.0.0:8000
```

### Terminal B & C — two synthetic robots, distinct namespaces, SAME topic name

```bash
source /opt/ros/jazzy/setup.bash && export ROS_LOCALHOST_ONLY=1
cd /home/admin/Downloads/biaozhul/P048/a && export PYTHONPATH="$PWD/src:${PYTHONPATH:-}"
mr-synthetic-robot alpha --namespace team/alpha --topics cmd/move   # terminal B
mr-synthetic-robot beta  --namespace team/beta  --topics cmd/move   # terminal C
```

### Terminal D — drive it with curl

```bash
BASE=http://127.0.0.1:8000
ADMIN="X-Admin-Token: change-me-admin-token"

# 1) register robot id -> namespace (only registered namespaces are usable)
curl -s -X POST "$BASE/admin/robots/alpha" -H "$ADMIN" -H 'Content-Type: application/json' \
  -d @examples/register_alpha.json
curl -s -X POST "$BASE/admin/robots/beta"  -H "$ADMIN" -H 'Content-Type: application/json' \
  -d @examples/register_beta.json

# 2) mint a tester token (HMAC-signed; bound to robot + tester + mapping epoch)
TA=$(curl -s -X POST "$BASE/admin/robots/alpha/tokens" -H "$ADMIN" -H 'Content-Type: application/json' \
  -d @examples/token_request.json | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')

# 3) send a real command: target + sequence + TTL + tester identity
curl -s -X POST "$BASE/robots/alpha/commands" \
  -H "Authorization: Bearer $TA" -H 'Content-Type: application/json' \
  -d @examples/command_move.json
```

Watch terminal B: only **alpha** prints the envelope; beta stays silent.

### Watch events (SSE)

```bash
# per-robot (isolated; needs that robot's bearer token)
curl -N -H "Authorization: Bearer $TA" "$BASE/robots/alpha/events"
# global operator view (admin)
curl -N -H "$ADMIN" "$BASE/admin/events"
```

### Query one robot's isolated state

```bash
curl -s -H "Authorization: Bearer $TA" "$BASE/robots/alpha/state"
```

---

## 6. Acceptance (one command, real processes)

`scripts/acceptance.sh` boots the gateway + two synthetic robots and verifies
with curl: unicast, same-name topic isolation, expiry drop + audit reason,
sequence rollback, cross-robot token reuse, absolute-topic escape, epoch
invalidation after remap, and query isolation.

```bash
cd /home/admin/Downloads/biaozhul/P048/a
bash scripts/acceptance.sh
```

Expected tail:

```
================= ACCEPTANCE SUMMARY =================
PASS=NN FAIL=0
```

Logs (gateway, per-robot stdout, JSONL audit) are written under
`scripts/_run/`.

---

## 7. Automated tests

```bash
cd /home/admin/Downloads/biaozhul/P048/a
source /opt/ros/jazzy/setup.bash
export ROS_LOCALHOST_ON=1
PYTHONPATH="$PWD/src:${PYTHONPATH:-}" python3 -m pytest -q
```

* `tests/test_naming.py`    – absolute/traversal/private-name rejection, id rules
* `tests/test_auth.py`      – real HMAC sign/verify, tamper/expiry/**epoch** cases
* `tests/test_registry.py`  – epoch bump on remap, monotonic per-target sequences
* `tests/test_integration.py` – **real DDS** between the gateway bridge and two
  synthetic rclpy nodes, plus a **real uvicorn** server for SSE:
  unicast, reconnection/latching, same-name topics, forged/cross-robot/
  mismatched-tester requests, topic-escape, expiry+audit, rollback/duplicate,
  remap invalidation, deregistration, query isolation, SSE isolation and the
  immediate `mapping_reset` stream cut-off.

> SSE tests use a real TCP socket rather than httpx's in-process ASGI
> transport: httpx 0.28 buffers a whole ASGI response body and therefore
> cannot observe an infinite event stream. All other HTTP tests use the
> in-process transport; everything still executes the genuine app and rclpy
> code paths.

---

## 8. HTTP API

### Admin (header `X-Admin-Token`)
| Method & path | Purpose |
|---|---|
| `POST /admin/robots/{robot_id}` | register / remap id→namespace (bumps `epoch`, disposes old publishers, cuts SSE streams) |
| `DELETE /admin/robots/{robot_id}` | deregister (revokes all tokens & streams) |
| `GET  /admin/robots` | list robots, epochs, sequence watermarks |
| `POST /admin/robots/{robot_id}/tokens` | issue an HMAC token for a tester identity |
| `GET  /admin/events` | global SSE view (`X-Admin-Token`, or `?token=`) |

### Robot (header `Authorization: Bearer <token>`)
| Method & path | Purpose |
|---|---|
| `POST /robots/{robot_id}/commands` | publish a command into that robot's namespace |
| `GET  /robots/{robot_id}/state` | that robot's namespace/epoch/sequence state only |
| `GET  /robots/{robot_id}/events` | that robot's SSE stream only |

### Command body

```json
{
  "target": "cmd/move",      // RELATIVE topic; '/x', '../y', '~z' are rejected
  "sequence": 1,             // monotonic per (robot, target); no rollback/replay
  "ttl_seconds": 30,         // validity window (or use absolute "expires_at")
  "tester_id": "tester-alice", // must equal the identity bound to the token
  "payload": { "action": "goto", "x": 1.25 }
}
```

The ROS message (`std_msgs/String`, JSON) published to
`/<namespace>/<target>` is:

```json
{"v":1,"rid":"alpha","ns":"team/alpha","target":"cmd/move","seq":1,
 "tester_id":"tester-alice","issued_at":…,"expires_at":…,"payload":{…}}
```

### Rejection reasons (also written to the JSONL audit log)
`missing_bearer_token` · `token_malformed` · `token_signature_invalid` ·
`token_expired` · `stale_authorization` (mapping changed) ·
`tester_mismatch` · `bad_topic` (absolute/escape) · `validity_missing` ·
`validity_conflict` · `command_expired` · `sequence_rollback` ·
`sequence_duplicate` · `robot_not_registered`.

---

## 9. Configuration

| Env var | Default | Meaning |
|---|---|---|
| `MRGW_HMAC_SECRET` | dev-only value | HMAC-SHA256 signing key — **set in production** |
| `MRGW_ADMIN_TOKEN` | dev-only value | shared secret for the admin API |
| `MRGW_TOKEN_TTL_SECONDS` | `3600` | default token lifetime |
| `MRGW_AUDIT_LOG` | `mr_gateway_audit.jsonl` | append-only audit log path |
| `MRGW_HOST` / `MRGW_PORT` | `0.0.0.0` / `8000` | bind address |
| `ROS_DOMAIN_ID` / `ROS_LOCALHOST_ONLY` | — | standard ROS 2 DDS isolation |

---

## 10. Security notes

* The symmetric HMAC model is intentionally simple: a deployment that needs
  per-tester private keys should replace the verify path in `auth.py`; the
  `epoch`/namespace-binding contract stays the same.
* The gateway never accepts a fully-qualified topic from callers. Even if an
  attacker controls `target`, `naming.validate_target` rejects leading `/`,
  `~`, `.`/`..` and empty segments, and `qualify_topic()` always re-validates
  and re-prefixes inside the registered namespace.
* On remap the old publishers are destroyed, so the process cannot keep
  emitting into a namespace it no longer owns.
