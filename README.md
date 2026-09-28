# adam-shards

Local **multiprocess Adam-state sharded checkpointing** with **resharding
across a different number of processes**. Parameters are identified by a
stable **name + shape** (never by traversal position), first/second moments and
the per-parameter step stay bound together, and a checkpoint whose manifest is
incomplete, misshapen, tampered, or whose model/optimizer parts came from
different commits is **refused at load time**.

Everything runs locally with synthetic fixtures — no accounts, no external
services, no real data.

---

## 1. What it does

1. Trains a small ReLU MLP (closed-form softmax-cross-entropy gradients).
2. Flattens all parameters into one canonical index space (sorted by name) and
   partitions it across `N` **writer OS processes**, one `.bin` file per shard
   per tensor kind (`param`, `moment1`, `moment2`).
3. Atomically publishes a `manifest.json` that records, per shard: file name,
   size, SHA-256 digest, and the exact flat slices it covers; plus a digest
   for the **model** and **optimizer** parts bound to one `commit_id`.
4. Restores with `M` **reader processes** where `M` may differ from `N`
   (shard files are handed out round-robin). The restore-side parameter
   identity may be presented in a **scrambled order**; values are still keyed
   by name.
5. Takes one more optimizer step and asserts the result is identical to a
   fully independent, **unsharded reference run**.

The required acceptance case — **save with 2 processes, restore with 3, with
parameters reordered, and verify one post-restore update equals the
reference** — is exercised end-to-end (`tests/test_pipeline.py`,
`POST /verify/pipeline`, and the CLI).

---

## 2. Layout

```
src/adam_shards/
  tensor_types.py   # ParamId (name+shape), immutable Tensor, SHA-256 digests
  errors.py         # typed failure categories (missing_shard, digest_mismatch…)
  graph.py          # stably-named MLP, exact forward/backward, synthetic data
  adam.py           # bias-corrected Adam, name-keyed OptimState (m,v,step)
  sharding.py       # multiprocess save/load/reshard, manifest, all validation
  verification.py   # independent reference Adam + finite-difference oracle
  pipeline.py       # train → shard(2) → restore(3)+reorder → one step → verify
  api.py            # FastAPI app (request-id correlation, typed errors)
  server.py         # uvicorn entry point
  config.py         # JSON config loading/validation
  logging_utils.py  # request-id-aware logging adapter
  __main__.py       # CLI report
configs/default.json
examples/           # sample dataset generator + on-disk reshard demo
tests/              # unit + integration + HTTP + CLI tests
```

The independent reference in `verification.py` never imports the production
optimizer/graph/sharding code — it re-derives forward, backward and a
deliberately differently-shaped Adam update from scratch, so the expected
answers are not produced by the code under test. It is additionally anchored
to a hand-computed 2-class scalar answer in
`tests/test_pipeline.py::test_reference_oracle_matches_hand_derived_scalar_adam_step`.

---

## 3. First-time setup

Requires Python ≥ 3.10. Dependencies used during development:
NumPy 2.x, FastAPI, uvicorn, pytest, httpx.

```bash
cd b
python3 -m venv .venv && source .venv/bin/activate  # optional
pip install -r requirements.txt
# (or use an interpreter that already has numpy/fastapi/pytest/httpx)
```

No install step is required for the package itself — `pyproject.toml` puts
`src` on `pytest`'s path, and the example/CLI commands set `PYTHONPATH=src`.

Generate the deterministic sample dataset (already committed under
`examples/sample_dataset.npz`; regenerate any time):

```bash
PYTHONPATH=src python3 examples/generate_sample_data.py
# wrote examples/sample_dataset.npz: x=(40, 3) y=(40,) classes=[0, 1]
```

---

## 4. Run the tests

```bash
python3 -m pytest -q
```

Real result on this machine:

```
52 passed, 1 warning in ~12s
```

With coverage:

```bash
python3 -m pytest --cov=src/adam_shards --cov-report=term -q
```

Core-module coverage is ≥ 86% (overall ≈ 87%); the only 0% files are the thin
`server.py` launcher and the CLI `__main__.py` presentation layer (the CLI is
exercised in a subprocess by `tests/test_cli_api.py`).

---

## 5. Run the CLI verification

```bash
PYTHONPATH=src python3 -m adam_shards --config configs/default.json \
    --save-world 2 --restore-world 3
```

The exit code is `0` only if parameters, moment1 and moment2 all match the
independent reference within tolerance. Real summary:

```
=== SUMMARY ===
request_id          : cli-demo
commit_id           : c88787890c662fc8
save/restore procs  : 2 -> 3
step after restore  : 5
finite-diff max rel : 4.516e-10
parameter_comparison  : passed=True max_abs_diff=2.776e-17 failures=[] uncertainties=[...]
moment1_comparison    : passed=True max_abs_diff=0.000e+00 failures=[] uncertainties=[]
moment2_comparison    : passed=True max_abs_diff=4.235e-22 failures=[] uncertainties=[]
OVERALL PASSED      : True
```

`step after restore = 5` confirms 4 sharded training steps plus exactly one
post-restore step. The finite-difference number is the independent numeric
gradient oracle's worst relative error on significant coordinates.

---

## 6. Inspect an on-disk reshard (2 → 5)

```bash
PYTHONPATH=src python3 examples/demo_reshard.py
```

Real result:

```
restored step: 4 source world: 2
new commit: 2b74ad8ee3b57e57 new world_size: 5
```

The 26 flat elements are redistributed from 2 shards to 5 shards (lengths
`6,5,5,5,5` — the uneven tail is handled and a parameter may straddle a
boundary). Inspect `.checkpoints/saved_2proc/manifest.json` and
`.checkpoints/resharded_5proc/manifest.json`.

---

## 7. Run the HTTP service

```bash
PYTHONPATH=src ADAM_SHARDS_CONFIG=configs/default.json \
    python3 -m uvicorn adam_shards.server:app --port 8911
```

```bash
curl -s -H "X-Request-ID: demo-1" localhost:8911/health

curl -s -X POST localhost:8911/verify/pipeline \
  -H "X-Request-ID: demo-2to3" -H "Content-Type: application/json" \
  -d '{"save_world_size":2,"restore_world_size":3,"train_steps":4,"tol":1e-12}'

# materialize a new shard count on disk
curl -s -X POST localhost:8911/checkpoints/reshard \
  -H "X-Request-ID: demo-reshard" -H "Content-Type: application/json" \
  -d '{"src_dir":".checkpoints/saved_2proc","dst_dir":".checkpoints/api_4","new_world_size":4}'

curl -s -X POST localhost:8911/checkpoints/manifest \
  -H "Content-Type: application/json" \
  -d '{"ckpt_dir":".checkpoints/saved_2proc"}'
```

Every response and every log line carries the **request id** (client
`X-Request-ID`, or a generated `req-…`). Example correlated log lines:

```
… INFO adam_shards [request_id=demo-2to3][pipeline] save commit start world_size=2 total=26 ranges=[(0, 13), (13, 26)]
… INFO adam_shards [request_id=demo-2to3][pipeline] writer pid=43723 rank=0 wrote 3 kinds
… INFO adam_shards [request_id=demo-2to3][pipeline] restore start source_world=2 reader_world=3 total=26
```

Hard failures return HTTP 422 with a specific `category`; uncertain
conclusions (e.g. probes landing on near-zero gradients, or a same-count
restore that did not exercise resharding) are returned separately under
`uncertainties`, never mixed into `failures`.

---

## 8. Failure categories (each has a dedicated test)

| category | raised when | test |
|---|---|---|
| `missing_shard` | manifest absent, or a listed shard file missing/unreadable | `test_missing_shard_file_is_rejected` |
| `incomplete_manifest` | shard ranks not `0..N-1`, slices don't exactly cover `[0,total)`, overlap, or a hole | `test_incomplete_manifest_dropped_shard_is_rejected`, `…_truncated_slice…`, `test_overlapping_coverage_is_rejected` |
| `shape_mismatch` | restore graph declares a different shape for a name | `test_wrong_shape_in_restore_graph_is_rejected` |
| `parameter_unknown` | restore graph's name set differs from the checkpoint | `test_parameter_identity_set_mismatch_is_rejected` |
| `digest_mismatch` | shard bytes tampered, **step** tampered (optimizer summary), or layout/model summary altered | `test_corrupt_shard_bytes_fail_summary_digest`, `test_corrupt_step_in_manifest_fails_optimizer_summary`, `test_corrupt_layout_in_manifest_fails_model_summary` |
| `commit_mismatch` | model and optimizer parts do not share one `commit_id` | `test_model_optimizer_commit_mismatch_is_rejected` |
| `corrupt_manifest` | manifest JSON unparseable / structurally invalid / unsupported version | `test_corrupt_manifest_json_is_rejected` |

### Why these checks

- **Incomplete manifest ⇒ refuse.** Coverage is computed as a boolean mask over
  the full flat range per tensor kind; any hole or overlap is rejected before
  any data is assembled, so a partial checkpoint can never silently yield
  zero-filled state.
- **Model + optimizer are one commit.** The manifest is the single atomic
  publish point (`os.replace` of a temp file). Each part stores the same
  `commit_id` and a digest covering layout, file digests, and (for the
  optimizer) per-parameter steps; a mismatched id or recomputed digest fails.
- **m, v, step cannot be cross-paired.** `MomentRecord` and the manifest digest
  bind the triple to the parameter name; restore reassembles by named offsets.

---

## 9. Configuration

`configs/default.json` controls dims, seed, batch/steps, Adam hyperparameters,
default save/restore world sizes and tolerances. Invalid hyperparameters
(zero/negative `lr`/`eps`, `beta` outside `[0,1)`) are rejected at
construction.

---

## 10. Notes on determinism and processes

- Save uses `N` spawned writer processes; restore uses `M` spawned reader
  processes. The `spawn` start method is used so workers are independent
  interpreters (safe under the multi-threaded ASGI server).
- Float payloads are stored little-endian `float64`; round-trip equality in
  tests is exact (`array_equal`) at the shard layer and machine-tight
  (`atol≈1e-12`) against the independently computed Adam trajectory.
- All fixtures are local and deterministic (fixed seeds).
