# ROS Replay Checkpoint Service

A pure back-end service for deterministic, controllable **ROS 2 bag replay**,
built with Python, `rosbag2_py` and FastAPI. It replays local test bags against
their recorded timestamps with stable per-message sequence numbers, and
supports pause, variable playback speed, seeking, crash-safe checkpoints and
restart-resume. A jump (seek) starts a **new replay generation**: messages
queued by the old generation are provably never published afterwards.

There is no front-end; control is a small JSON HTTP API.

---

## 1. Design at a glance

```
HTTP (FastAPI) ──► SessionManager ──► ReplayEngine (one thread/session)
                         │                  │  canonical index + timing heap
                         │                  ▼
                         │            MultiSink ──► RingSink   (in-memory, tests/API)
                         │                       └► RosSink    (raw CDR over DDS)
                         ▼
                  CheckpointStore (atomic JSON + SHA-256 source digests)
```

### Deterministic ordering and stable sequence numbers
`rosbag2_py` does not guarantee cross-topic order for equal timestamps (the MCAP
plugin logs that published-timestamp ordering is unimplemented). So on load the
service reads every message once and builds a **canonical global index** sorted
by

```
(timestamp_ns, topic_name, native_read_position)
```

Each message gets a stable, zero-based **sequence number** = its position in
that order. Pause / speed / seek all operate on this index, so ordering is fully
under service control and identical across reloads. Messages that share a
timestamp are emitted as a contiguous block in that exact order.

### Time semantics — speed never rewrites timestamps
When playing, the first pending message is anchored to wall-clock now and each
subsequent due time is

```
due = anchor_wall + (bag_timestamp(pos) - bag_timestamp(anchor_pos)) / rate
```

Changing `rate` only scales the *playback clock*; the relative timing of
messages is preserved and every emitted record carries the **original bag
timestamp** untouched.

### Generations and the cancellation race
Each session tracks two positions:

* `emitted_pos` — the next message to actually publish;
* `scheduled_pos` — the next message placed in the timing heap.

The timing heap is bounded (`LOOKAHEAD`). Every heap item is stamped with the
**generation** that scheduled it. Immediately before publishing, the engine
re-checks the generation under a lock:

* **pause / rate** do not move `emitted_pos` and do **not** bump the generation;
  they flush the heap and re-schedule the unemitted remainder;
* **seek** sets both positions to the target and bumps the generation. Any
  already-queued item from the prior generation fails the re-check and is
  counted as `dropped` — it can never be published, even if it was microseconds
  away from going out.

This is tested deterministically at the claim point
(`test_claim_rejects_stale_generation_directly`) and under rapid seeks, plus
end-to-end in the acceptance script.

### Checkpoints and source integrity
A checkpoint records the bag summary, the topic filter, the precise next
message position, rate/state/generation and a **SHA-256 digest of every bag
file** (data files *and* `metadata.yaml`). On restore the files are rehashed;
any addition, removal or modification makes restore fail with **409** — a
position is meaningless against a changed bag. Checkpoints are written
atomically (temp file + `fsync` + `os.replace`).

---

## 2. Requirements

* Ubuntu with **ROS 2 Jazzy** installed at `/opt/ros/jazzy` (provides
  `rclpy`, `rosbag2_py`, `std_msgs`, `example_interfaces`).
* Python 3.12 (the Jazzy Python).
* The pip dependencies in `requirements.txt` (FastAPI, uvicorn, pydantic,
  pytest, httpx).

> The non-DDS test-suite and API run without a network/DDS backend. Set
> `ROS_ENABLED=false` to disable real publishing entirely (the in-memory ring
> sink still records every emission).

---

## 3. Install

```bash
cd /path/to/P043/b
source /opt/ros/jazzy/setup.bash
python3 -m pip install -r requirements.txt        # versions are locked
# optional: install the package itself for the `ros-replay` console script
python3 -m pip install -e .
```

---

## 4. Generate the example bag

```bash
source /opt/ros/jazzy/setup.bash
python3 scripts/generate_bag.py --uri bags/demo --seconds 1
```

The demo bag contains:

| topic     | type                          | rate | same-timestamp pair |
|-----------|-------------------------------|------|---------------------|
| `/tick`   | `example_interfaces/msg/Int64`| 10 Hz| written *after* `/tock` |
| `/tock`   | `example_interfaces/msg/Int64`| 10 Hz| same timestamp as `/tick` |
| `/events` | `example_interfaces/msg/Int64`| 20 Hz| — |

`/tock` is physically written before `/tick`, yet replay emits `/tick` first
(canonical topic order) — proving ordering is not storage/write order. The
Int64 payload is the message's logical counter, which the verifier checks.

Example request bodies for every endpoint are in
[`examples/api_requests.json`](examples/api_requests.json).

---

## 5. Run the service (local start)

```bash
source /opt/ros/jazzy/setup.bash
export BAG_ROOT="$PWD/bags"                 # bags must live under this dir
export CHECKPOINT_DIR="$PWD/state/checkpoints"
export ROS_ENABLED=true                      # set false to disable DDS
export TOPIC_PREFIX="/replay"                # publishes on /replay/<topic>
python3 -m uvicorn --app-dir src rosreplay.api:app --host 127.0.0.1 --port 8000
# or, after `pip install -e .`:  ros-replay --host 127.0.0.1 --port 8000
```

Interactive API docs are at `http://127.0.0.1:8000/docs`.

### Quick manual walkthrough

```bash
BASE=http://127.0.0.1:8000

curl -s -X POST $BASE/bags/info -H 'Content-Type: application/json' -d '{"uri":"demo"}'

SID=$(curl -s -X POST $BASE/sessions -H 'Content-Type: application/json' \
  -d '{"uri":"demo","rate":1,"play":false}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["session_id"])')

curl -s -X POST $BASE/sessions/$SID/resume            # start (1x)
curl -s     $BASE/sessions/$SID/status                # observe progress
curl -s -X POST $BASE/sessions/$SID/pause             # pause
curl -s -X POST $BASE/sessions/$SID/rate  -H 'Content-Type: application/json' -d '{"rate":4}'
curl -s -X POST $BASE/sessions/$SID/resume            # resume at 4x
curl -s -X POST $BASE/sessions/$SID/seek  -H 'Content-Type: application/json' -d '{"seq":40,"play":true}'
curl -s     "$BASE/sessions/$SID/published"           # ring sink record
```

---

## 6. HTTP API

| Method | Path | Purpose |
|--------|------|---------|
| GET  | `/health` | Liveness |
| POST | `/bags/info` | Bag summary: topics, counts, time range, file digests |
| POST | `/sessions` | Create a replay session (`uri`, `topics?`, `rate`, `play`) |
| GET  | `/sessions/{id}/status` | State, generation, rate, cursor, next seq/timestamp, counters |
| POST | `/sessions/{id}/pause` | Pause (position kept) |
| POST | `/sessions/{id}/resume` | Resume |
| POST | `/sessions/{id}/rate` | Set playback speed (`>0`) |
| POST | `/sessions/{id}/seek` | Jump by `seq` (filtered position) **or** `timestamp_ns` (lower bound); `play?` |
| GET  | `/sessions/{id}/published` | Ring-sink records (`?generation=&since_seq=`) |
| DELETE | `/sessions/{id}` | Tear a session down |
| POST | `/sessions/{id}/checkpoints` | Save a checkpoint (optional `checkpoint_id`) |
| GET  | `/checkpoints` / `/checkpoints/{id}` | List / fetch checkpoints |
| POST | `/checkpoints/restore` | Restore + resume (`checkpoint_id`, `play?`); **409 if the bag changed** |

Status fields include `generation` (bumped on every seek), `next_seq` /
`next_timestamp_ns` (the next message to publish; `null` at the tail),
`published_count` and `dropped_count` (stale-generation items discarded).

Error model: invalid/corrupt bag → **400**; unknown session/checkpoint →
**404**; restoring against a changed bag → **409**; malformed request → **422**.

---

## 7. Tests

```bash
source /opt/ros/jazzy/setup.bash
python3 -m pytest                       # ring-only: deterministic, no DDS
RUN_DDS_TESTS=1 python3 -m pytest       # additionally: real rclpy pub/sub test
```

Coverage includes:

* canonical same-timestamp ordering and stable sequences (`tests/test_bagio.py`);
* corrupt bag, missing `metadata.yaml`, digest add/remove/change detection;
* pause halts emission, resume continues with no duplicates/gaps;
* rate change scales spacing but never rewrites timestamps;
* seek bumps the generation and stale queued messages are dropped (direct claim
  test, tail jump, rapid-seek stress);
* **checkpoint → fresh manager (simulated restart) → resume at saved position**;
* restore rejected after the bag is tampered with or a file removed;
* full HTTP surface with FastAPI `TestClient` (`tests/test_api.py`);
* opt-in live DDS round-trip (`tests/test_dds.py`).

### One-shot acceptance (running server + live subscriber)

```bash
source /opt/ros/jazzy/setup.bash
RUN_DDS_VERIFY=1 PORT=8011 bash scripts/acceptance.sh
```

This regenerates the bag, starts uvicorn, drives pause/rate/seek/tail-seek over
HTTP, restarts the service from a checkpoint, tampers the bag to prove restore
is refused, and finally runs the live subscriber node to verify ordered
delivery over real DDS. Set `RUN_DDS_VERIFY=0` to skip the DDS portion.

### Standalone subscriber verifier

```bash
# with a 1x replay of /tick,/tock running on the service:
python3 scripts/verify_subscriber.py --duration 5 --expect tick=11,tock=11
```

It checks monotonic, gap-free counters and the canonical same-timestamp order.

---

## 8. Project layout

```
src/rosreplay/
  config.py        # env settings, BAG_ROOT confinement
  bagio.py         # SHA-256 digests, bag validation, canonical message index
  sinks.py         # RingSink (tests/API) + RosSink (raw CDR over DDS)
  engine.py        # scheduler: timing, pause/rate/seek, generation guard
  checkpoints.py   # atomic checkpoint persistence + source verification
  ros_runtime.py   # lazy rclpy init (single context, no background executor)
  sessions.py      # owns bags/engines/sinks/checkpoints
  api.py           # FastAPI app and routes
  main.py          # uvicorn entry point
scripts/
  generate_bag.py      # deterministic demo bag generator
  verify_subscriber.py # live DDS verification node
  acceptance.sh        # end-to-end acceptance against a running server
tests/                 # pytest suite (bagio, engine, checkpoints, api, dds)
examples/
  api_requests.json    # example request bodies
```

---

## 9. Notes & honest limitations

* The engine loads the whole bag into memory (raw CDR per message), which is
  appropriate for the stated **small local test bags**. Multi-gigabyte
  production bags would need a paged index; the deterministic-index design
  extends naturally to storing offsets instead of bytes.
* BAG paths are confined to `BAG_ROOT`; a path resolving outside it is rejected.
* Real DDS publishing degrades gracefully: if `rclpy`/a message package is
  unavailable while `ROS_ENABLED=true`, the session still runs ring-only and
  reports `ros_publishing: false` in status rather than failing.
