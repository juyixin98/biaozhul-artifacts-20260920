# Synthetic Organization-Name Entity Resolution

A backend that resolves which synthetic organization-name records refer to the
same real entity. It combines **candidate matching** (explainable pairwise
evidence) with **globally constrained clustering** (a correlation-clustering
objective under hard `must-link` / `cannot-link` constraints and human locks).

The central design rule is stated once and enforced everywhere:

> **Pairwise similarity is not transitive.** A high A–B and B–C score says
> nothing about A–C. Similarity is soft evidence fed to a global objective;
> only explicit constraints are hard, and they are checked for contradiction
> before they are applied. A shared or identical name is never, by itself, a
> declaration of identity.

---

## 1. What it guarantees

- **No threshold connected components.** Every pair score (including weak
  pairs) enters a global correlation-clustering objective, so the optimum can
  split a similarity chain. See `tests/test_clustering_oracle.py`.
- **Hard constraints, validated first.** `must-link` / `cannot-link` are
  checked for direct and transitive (`A~B, B~C,` but `A≄C`) contradictions and
  for conflicts with locked clusters *before* any clustering runs.
- **Same name ≠ same entity.** Disagreeing hard attributes (e.g. a registered
  id) veto a name-based candidate; a `cannot-link` overrides an identical name.
- **Clusters with provenance.** Each output cluster carries the per-pair
  evidence supporting it; non-obvious rejections carry an explicit reason.
- **Lockable human mappings.** A confirmed cluster can be locked; later
  resolves preserve it, and two separately locked clusters are never merged.
- **Affected entities after a change.** Mutations return which records moved
  and which clusters were created / dissolved / modified (from an audit log).
- **Reference-grade small cases.** For small instances the solver enumerates
  *all* restricted-growth partitions and returns the objective optimum.
  Tests cross-check it against an **independently written** brute-force oracle
  (`tests/oracle.py`) that never imports the production core.
- **Replayable diagnostics.** Every run gets a sortable `run_id`; its journal
  records inputs (ids), intermediate canonical forms/scores, the constraint
  blocks, each decision with a reason, and the terminal error category.

---

## 2. Architecture and module boundaries

```
entity_resolution/
  config.py         engine configuration (env overridable)
  errors.py         error taxonomy + categorical exception contract
  models.py         pydantic data/error contracts (the wire boundary)
  normalization.py  alias index + cross-language canonicalization  [语料规范化]
  similarity.py     pairwise scoring + explainable candidate evidence [候选匹配]
  clustering.py     constraint validation + exact/greedy mining core  [挖掘内核]
  storage.py        SQLite: records, links, clusters, locks, audit   [索引与模型]
  diagnostics.py    run journals (JSONL)                             [诊断]
  validation.py     query-string validation                          [查询验证]
  service.py        orchestration facade + change-impact             [用例编排]
  api.py            FastAPI transport + error-envelope mapping       [接口]
tests/
  oracle.py         INDEPENDENT brute-force partition oracle
  test_*.py         unit + integration + HTTP + diagnostics tests
fixtures/corpus.example.json   synthetic corpus (alias/CJK/Cyrillic/constraints)
scripts/demo.py                library-level end-to-end demo
scripts/http_example.sh        curl walkthrough
```

Data contract: only `models.py` structures cross boundaries. Error contract:
every layer raises a subclass of `EntityResolutionError` from `errors.py`; the
API maps them to one stable envelope.

### Mining core (`clustering.py`)

- `validate_constraints(...)` → rejects unknown records/self links (input),
  direct contradictions, must-link closure contradictions, cannot-links inside
  locked blocks, and must-links spanning two locks (state conflicts).
- `solve(...)`:
  - collapses must-links and locked blocks into atomic blocks,
  - injects implicit cannot-links **between different locked clusters**,
  - **exact mode**: restricted-growth enumeration of every feasible partition,
    Bell-number bounded (`ResourceExhaustedError` past budget),
  - **auto mode**: exact while within budget, otherwise a deterministic
    agglomerative heuristic minimizing the *same* objective (merge gain
    `2·Σw − |Ci|·|Cj|`; cannot-crossing clusters never merge).

Objective for a pair with similarity `w`: cost `1−w` if co-clustered, `w` if
separated; a cannot pair co-clustered is infeasible (+∞).

### Candidate matching (`similarity.py` + `normalization.py`)

- NFKC + case/punctuation/whitespace folding; legal-form suffixes removed only
  as whole tokens (so "Atlas Copco" is not damaged by "Co").
- Deterministic Cyrillic transliteration and a small CJK variant map; genuine
  cross-language bridges come from the explicit **alias table**.
- Score is an explainable blend (`token_overlap`, `char_similarity`,
  `alias_match`, attribute agree/conflict) — never a single opaque number.

---

## 3. Data contract (corpus spec)

`POST /corpus` accepts a `CorpusIn`:

```json
{
  "records": [
    {"id": "r1", "name": "Acme Bank Ltd", "language": "en",
     "attributes": {"reg_id": "REG-1", "country": "US"}}
  ],
  "must_links": [["r1", "r2"]],
  "cannot_links": [["r1", "r3"]],
  "aliases": {"Gazprom": ["Газпром", "GAZPROM"]}
}
```

- `records[].id` is the primary key (unique, non-empty).
- `attributes` are structured signals; keys configured as hard attributes
  veto candidacy on disagreement. Name text is never treated as an identifier.
- `aliases` maps one canonical spelling to alternative spellings (used for
  cross-language / abbreviation bridging, not identity).

See `fixtures/corpus.example.json` for a worked corpus covering an A–B–C
similarity chain, a Cyrillic/Latin alias bridge, and a same-name/different-id
case.

---

## 4. Error categories (distinguishable end to end)

| Category             | HTTP | Example codes |
|----------------------|------|---------------|
| `INPUT_ERROR`        | 400 / 404 | invalid payload, empty corpus, unknown record, bad threshold |
| `STATE_CONFLICT`     | 409  | `CONSTRAINT_CONFLICT`, `LOCK_VIOLATION`, `VERSION_CONFLICT` |
| `RESOURCE_EXHAUSTED` | 507  | exact partition budget / Bell bound exceeded |
| `COMPUTATION_FAILED` | 500  | unexpected internal failure (no internals leaked) |

Every error uses one envelope:

```json
{"error": {"category": "STATE_CONFLICT",
           "code": "STATE_CONFLICT:CONSTRAINT_CONFLICT",
           "message": "cannot-link endpoints are forced together",
           "details": {"pair": ["A", "C"], "reason": "must_link_closure"},
           "run_id": null}}
```

---

## 5. Running it

Requirements are pinned in `requirements.txt` (Python 3.12).

```bash
python3 -m pip install -r requirements.txt

# tests (107 tests) with coverage
python3 -m pytest --cov=entity_resolution --cov-report=term-missing

# library-level demo against the synthetic fixture
python3 scripts/demo.py

# HTTP server (persistent SQLite)
ER_DB_PATH=data/demo.sqlite3 \
  python3 -m uvicorn entity_resolution.api:app --host 127.0.0.1 --port 8000
# then, in another shell:
bash scripts/http_example.sh
```

### HTTP endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET  | `/health` | liveness + record count |
| POST | `/corpus` | load/replace a corpus spec |
| GET  | `/records` | records with current assignment/lock flags |
| POST | `/links` | add a must/cannot-link (conflict checked) |
| POST | `/resolve?threshold=` | run mining; clusters + evidence + rejections |
| GET  | `/clusters` | current persisted clusters |
| POST | `/clusters/lock` | lock a human-confirmed mapping (optional `expected_version`) |
| GET  | `/affected?run_id=` | change impact for the latest (or a given) run |

### Configuration (env)

`ER_DB_PATH`, `ER_LOG_DIR`, `ER_THRESHOLD`, `ER_SOLVER_MODE` (`auto`/`exact`),
`ER_MAX_EXACT_PARTITIONS`. Hard-attribute keys are set when constructing
`SimilarityConfig` (see `scripts/demo.py`).

---

## 6. Replaying a problem from a journal

Each operation writes `logs/<run_id>.jsonl`. The resolve journal includes:

- `stages[].state` — per-pair canonical forms, scores, components,
  `candidate`/`vetoed`, and the solved blocks (`method`, `optimal`,
  `partitions_evaluated`);
- `decisions[]` — `soft_candidate`, `reject_hard_attribute_conflict`,
  `applied_with_locks_preserved`, … with the record pair as subject;
- `error` — category/code/details when the run fails.

Given a `run_id`, open `logs/<run_id>.jsonl` to reconstruct the inputs, the
intermediate candidate state, and the rationale for each merge/split.

---

## 7. Verification performed

- 107 tests pass; total coverage **94%** (every module ≥ 90%), asserting
  concrete clusters/scores and specific failure categories — not merely that
  endpoints are callable.
- Exact solver outputs are checked optimal & feasible against the independent
  oracle over 40 generated small problems plus fixed chain/lock cases.
- A chain case is shown to differ from the threshold-connected-components
  baseline.
- The API was exercised over real HTTP (uvicorn): load → resolve → 409
  contradiction → lock → re-resolve (lock preserved) → affected entities →
  400/404/507/500 categories.

---

## 8. Known limitations

- **Exact solving is intentionally small.** Enumeration is Bell-bounded;
  beyond `ER_MAX_EXACT_PARTITIONS` the system raises `RESOURCE_EXHAUSTED` in
  `exact` mode or uses the deterministic greedy heuristic in `auto` mode,
  which is not guaranteed globally optimal (it still enforces all hard
  constraints and optimizes the same objective).
- **Transliteration/CJK is minimal and table-driven** by design; it is a
  normalization aid, not a translator. Production-grade multilingual matching
  should extend the alias table (or plug in a dedicated normalizer behind
  `normalize_name`).
- **Single-process SQLite.** WAL + a process lock make the bundled server
  safe under uvicorn's thread pool, but this is not a multi-node store.
- **Scoring is lexical.** It deliberately uses no external services or models;
  phonetic/embedding signals could be added as additional evidence components
  without changing the clustering contract.
- Constraints are validated as a set per write; extremely large constraint
  batches pay the union-find/closure cost up front.
