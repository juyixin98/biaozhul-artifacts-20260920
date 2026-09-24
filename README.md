# Dependency-Lock Consistency Gate (`lockgate`)

A **pure-backend, offline** audit service for Node.js dependency locks. It
takes a project bundle (`package.json` + `package-lock.json`, npm
**lockfileVersion 3**), optionally with the referenced registry tarballs,
and decides whether the dependency closure is genuinely reproducible.

> **A lockfile merely existing is not reproducibility.**
> This gate treats a lock as reproducible only when every node is correctly
> *pinned* (resolved URL + valid integrity digest) **and** every shipped
> artifact's digest is *recomputed from bytes* and matches. A
> syntactically-perfect lock without artifacts reports
> `reproducible: false` — see scenario `10`.

The service never extracts uploads to disk and **never executes
install/lifecycle scripts**; every package with a lifecycle script gets an
explicit `install-script-not-run` finding so the report cannot be mistaken
for having run it.

## What it checks

| Area | Checks |
|---|---|
| Root declarations | every `dependencies` / `optionalDependencies` / `devDependencies` range in `package.json` resolves to a locked version that **satisfies** it (real SemVer evaluation, not string compare) |
| Lock structure | v3 layout only; v1/v2 explicitly refused; required `resolved` + `integrity`; valid semver versions |
| Integrity | strict SRI parsing (`sha256/384/512`, exact digest length); supplied tarballs are **re-hashed** with a real cryptographic digest and compared in constant time |
| Registry | `resolved` must be `https`, a `.tgz`, and on an explicit host allowlist (default `registry.npmjs.org`) |
| Dependency graph | Node-style upward `node_modules` resolution, unreachable/phantom nodes, shortest dependency chain (BFS) for every finding |
| Duplicates | same package pinned to >1 distinct version reported with all lock keys |
| Cycles | Tarjan SCC cycle detection, root-relative shortest chain per cycle |
| Workspaces | `workspaces` glob expansion, link-entry existence/`link:true`/target consistency, member dependency coverage |
| Optional/platform | `os`/`cpu`/`libc` constraints evaluated against an explicit target; an optional package excluded on target is `info`, a *required* one excluded is `error`; an optional dep absent on the target platform is accepted |
| Unsupported syntax | **explicitly rejected**, never guessed: `file:`, `git:`/`git+`, `github:`/shorthand, `http(s)` tarball URLs, `npm:` aliases, `link:`, `workspace:`, tags like `latest`, non-semver tokens, lockfileVersion ≠ 3 |
| Input safety | in-memory tar/zip only; absolute paths, `..` traversal, symlinks/hardlinks/devices, duplicate entries, member/size caps all refused |

## Findings and reproducibility

Each finding carries:

- `code` — stable machine code (`range-drift`, `artifact-tampered`, …)
- `severity` — `error` / `warning` / `info`
- `location` — exact JSON path inside the bundle
- `chain` — **shortest dependency chain** from the project root to the node
- `diff` — a reviewable unified diff (expected vs actual) where applicable
- `evidence` — raw expected/actual values used to reach the conclusion

`reproducibility` separates three questions:
`lockfile_present` ⊂ `pinned` ⊂ `content_verified` ⊂ `reproducible`.

## Requirements / layout

```
app/lockgate/        service code (semver engine, integrity, auditor, FastAPI)
tests/               115 automated tests (pytest)
examples/            10 generated input bundles (see below)
scripts/make_examples.py  builds examples with REAL tarballs and REAL digests
wheelhouse/          all dependency wheels for air-gapped install
requirements.txt     exact pins with sha256 hashes
```

## Local setup (offline, hash-pinned)

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install --no-index --require-hashes --find-links=wheelhouse -r requirements.txt
pip install --no-index --no-deps -e .
```

(If you are online and trust the environment, you can instead
`pip install -e ".[test]"` — but the locked/hash-pinned path above is the
delivered, verifiable one.)

## Start the service

```bash
uvicorn app.lockgate.web:app --host 127.0.0.1 --port 8000
```

Interactive docs: <http://127.0.0.1:8000/docs> · health: `GET /health`.

### Audit over HTTP

```bash
curl -s -F bundle=@examples/01-clean-lock/bundle.tar.gz \
     http://127.0.0.1:8000/api/v1/audit | jq

# evaluate against an explicit target platform (optional-dep modelling)
curl -s -F bundle=@examples/05-platform-optional/bundle.tar.gz \
     -F os=linux -F cpu=x64 \
     http://127.0.0.1:8000/api/v1/audit | jq
```

Form fields: `bundle` (required, `.zip`/`.tar`/`.tar.gz`), `os`+`cpu`
(+`libc`), `include_dev`, `allowed_registry` (comma-separated hosts),
`package_path`, `lock_path`.

A failed gate returns **HTTP 200** with `"ok": false` (the audit
succeeded; the input did not). Malformed or unsafe archives return
`4xx`. Nothing returns a fake "all clear".

### Audit on the command line

```bash
python -m app.lockgate.cli examples/06-cycle/bundle.tar.gz
python -m app.lockgate.cli examples/05-platform-optional/bundle.tar.gz --os linux --cpu x64
echo "exit code: $?"   # 0 = gate passed, 1 = errors found
```

## Acceptance commands

```bash
# 1) install exactly what is locked, offline, verifying every wheel hash
pip install --no-index --require-hashes --find-links=wheelhouse -r requirements.txt

# 2) run the full automated suite
python -m pytest tests/ -v

# 3) regenerate example inputs (real tarballs + real sha512 digests)
python scripts/make_examples.py

# 4) one-shot acceptance across every scenario
bash scripts/acceptance.sh
```

`scripts/acceptance.sh` boots uvicorn and asserts the expected verdict for
all ten scenarios, then tears the server down.

## Example scenarios

| Directory | Expected result |
|---|---|
| `01-clean-lock` | `ok`, `reproducible=true`, all artifacts verified |
| `02-lock-drift` | `range-drift` error + chain + diff |
| `03-duplicate-versions` | warning, `ms` at 2.1.3 **and** 3.0.0 |
| `04-missing-integrity` | `missing-or-bad-integrity` error, not pinned |
| `05-platform-optional` | windows-only optional dep → `info` exclusion on linux; lifecycle script flagged as not run |
| `06-cycle` | `cyclic-a ↔ cyclic-b` cycle with root chain |
| `07-unsupported-syntax` | `git+https` spec refused as unsupported syntax |
| `08-workspaces` | workspace link resolved & consistent |
| `09-tampered-artifact` | recomputed digest mismatch → `artifact-tampered` error |
| `10-lock-only-no-artifacts` | **perfectly pinned but `reproducible=false`** (thesis case) |

## Design notes & honest limitations

- The SemVer engine implements npm's documented range grammar (caret,
  tilde, X-ranges, hyphen, comparators, `||`, prerelease precedence).
  Anything outside it is a hard, visible refusal — the gate will not
  silently "make a guess".
- Artifact verification covers the tarballs **you supply** (looked up by
  their `resolved` filename under `vendor/`, `artifacts/`, or
  `node_modules/.cache/`). The service makes no network calls; it cannot
  fetch what wasn't uploaded, and says so explicitly rather than claiming
  verification.
- Only registry tarball nodes carry the integrity requirement; git/file/
  alias nodes are reported as `unsupported-syntax` and do not cascade
  secondary errors.
- Engine-range (`engines.node`) policy is modelled in the data model but
  not enforced as a gate error, because no runtime target is assumed.

## License

Provided as-is for the exercise; all cryptographic work uses Python's
standard `hashlib`/`hmac` and is performed for real.
