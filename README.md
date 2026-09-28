# Stratified Permuted-Block Random Allocation Service

A local, reproducible **stratified permuted-block random allocation**
service for experimental subjects, built with Python, FastAPI, NumPy,
SciPy and SQLite. Everything runs locally against synthetic fixtures — no
production accounts or real participant data.

It ships both a **working implementation** and **independent evidence that
the implementation is correct**.

---

## 1. What it guarantees

| Requirement | Where it lives |
|---|---|
| Stratified identity + frozen random seed | `stratblock/contract.py`, `stratblock/rng.py`, seed stored with the study at registration |
| Exact in-block ratio + explicit incomplete-tail handling | `plan_counts`, `TailPolicy`, `hamilton_counts`, `prefix_is_balanced` |
| Repeat request → original allocation; feature change cannot re-randomise | `storage.enroll` / `_handle_repeat` (`DUPLICATE_CONFLICT`) |
| Allocation concealment vs audit permission isolation | Bearer roles `enroller` / `auditor` / `administrator` in `stratblock/api.py` |
| Post-allocation significance is **not** treated as proof of correctness | every effect report sets `proves_allocation_correct=false` |
| Multi-module backend (contract, kernel, evidence, replay, …) | `stratblock/` + independent `reference/` |
| Enumerate small blocks, fixed-seed distribution, concurrency, tail closure, recovery | `tests/` and `demo/` |
| Independent tests assert concrete results and failure categories | expected values are frozen literals / an independent oracle |
| Interpretable results & logs tied to request identity | response envelope + structured JSON logs |

### Two tail policies

- **`permuted`** — classic permuted blocks: the whole block is shuffled up
  front. A complete block is exactly on ratio. If a stratum is sealed
  before a block fills, the realised prefix is reported. It is flagged
  `TAIL_BALANCE_VIOLATION` only when its counts **cannot realise the
  ratio** (the symmetric feasible-apportionment set), e.g. `(2,0)` in a
  1:1 block. A mere tie-break difference such as `(1,2)` vs `(2,1)` is not
  flagged.
- **`balanced_prefix`** — constrained allocation: at position `n` only
  arms below their deterministic Hamilton target are eligible, so every
  possible sealing point is on ratio (less randomness near boundaries;
  documented trade-off).

---

## 2. Repository layout

```
stratblock/
  config.py        frozen configuration (db, master seed, tokens)
  contract.py      statistical contract: StudyConfig, validation, errors,
                   Hamilton apportionment, symmetric prefix balance
  rng.py           stream derivation (HKDF-SHA256) + SHA256-counter PRNG
                   + pure allocation kernel (draw_next)
  storage.py       SQLite repository: frozen studies, idempotent enroll,
                   audit events, outcomes, concurrency lock
  estimator.py     difference in means, Welch t, exact/MC permutation test
  diagnostics.py   correctness evidence: block enumeration, two-way replay,
                   provenance, distribution checks, failures/uncertainties
  replay.py        reproducible scripts, audit-event replay, tail-closure experiment
  api.py           FastAPI app, role isolation, response envelope, logging
reference/
  pbr.py           INDEPENDENT standard-library-only oracle (no numpy/scipy,
                   no stratblock imports) implementing the same stream spec
tests/             62 independent pytest tests (unit/integration/concurrency/evidence)
demo/run_demo.py   end-to-end reproducible demo, writes evidence/output/*.json
docs/              API examples and the RNG specification
```

The reference oracle shares **no code** with the production kernel. Tests
cross-check the two against each other and against frozen vectors produced
once from the oracle plus a hand-written HKDF — the system never grades
itself.

---

## 3. Requirements / versions

Verified environment (see `requirements.txt`, pinned):

```
Python 3.12.3
numpy 2.4.6
scipy 1.15.3
fastapi 0.141.1
pydantic 2.13.5
uvicorn 0.54.0
httpx 0.28.1
pytest 9.1.1
```

Set up from a clean directory:

```bash
python3 -m venv .venv && source .venv/bin/activate  # optional
python3 -m pip install -r requirements.txt
```

(No `python-multipart` is needed; the API is JSON-only.)

---

## 4. Reproduce from a clean directory

```bash
# 1) run the full independent test suite
python3 -m pytest -q

# 2) run the end-to-end demo (regenerates demo/demo.db and evidence JSON)
python3 -m demo.run_demo

# 3) run the live API
STRATBLOCK_DB=demo/demo.db python3 -m uvicorn stratblock.api:create_app \
  --factory --host 127.0.0.1 --port 8080
# then follow docs/API_EXAMPLES.md
```

Running `python3 -m demo.run_demo` twice produces **byte-identical**
`evidence/output/` contents (verified), demonstrating seed/stream
reproducibility. The honestly recorded observed results (test counts,
coverage, artifact values, smoke-test HTTP codes, and bugs found and fixed
during development) are in [`docs/RESULTS.md`](docs/RESULTS.md).

Configuration via environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `STRATBLOCK_DB` | `demo/demo.db` | SQLite path (`:memory:` for ephemeral) |
| `STRATBLOCK_MASTER_SEED` | `20260927` | master seed for stream derivation |
| `STRATBLOCK_API_TOKENS` | `enrol-token:enroller\|audit-token:auditor\|admin-token:administrator` | `token:role` pairs joined by `\|` |

The master seed in force at study registration is stored with that study
and included in audit/replay provenance, so rotating the deployment seed
can never silently alter an already-running study.

---

## 5. The random stream (summary)

Per (study, stratum):

1. `stream_key = HKDF-SHA256(salt, master_seed_8B_BE, study_id NUL stratum_key NUL "v1")`
2. blocks are `SHA256(stream_key || counter_64_be)`; 8 big-endian 32-bit words each
3. `uniform(bound)` = rejection sampling; Fisher–Yates shuffles use it
4. each block consumes one word to choose its size, then the shuffle / constrained draws

The full bit-level specification is in `docs/RNG_SPEC.md`. The state is a
plain `(counter, word)` pair persisted per stratum, so a restart resumes
the stream exactly where it stopped.

---

## 6. Evidence produced

After the demo, inspect `evidence/output/`:

- `diagnostics_demo_permuted.json` — PASS, block counts, replay agreement, provenance
- `diagnostics_demo_balanced.json` — every prefix on its Hamilton target
- `diagnostics_demo_tail.json` — a genuinely infeasible `(2,0)` length-2 tail disclosed under `uncertainties`
- `replay_demo_permuted.json` — audit-event re-derivation PASS
- `effect_demo_permuted.json` — Welch + exact permutation results, with the scope caveat
- `distribution_fixed_seeds.json` — pooled arm counts over 8 fixed seeds, chi-square
- `seal_behavior.json` — post-seal enrollment returns `STRATUM_CLOSED`

Failures and uncertain/disclosed conditions are always separate top-level
lists; a disclosed permuted-tail imbalance is **not** counted as a
mechanism failure.

---

## 7. Interpretable logging

Each request gets an `X-Request-ID` (echo the client's `x-request-id` or a
generated one). Structured stderr log lines correlate the request id,
subject, study, arm decision, replay flag and service version, e.g.

```
{"event":"allocation_decision","request_id":"req-a-000","study_id":"demo-permuted",
 "subject_id":"P000","arm":"control","replayed":false,"actor":"enroller"}
```
