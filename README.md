# Recoverable Snapshot Controller

A pure-backend Kubernetes controller (Go + [controller-runtime](https://github.com/kubernetes-sigs/controller-runtime), tested on **kind**)
that produces real, content-derived digests of a source `ConfigMap`. Digests
are computed by an actual worker **Job** running on the cluster — not faked in
status — and status is always bound to a specific generation so stale work can
never overwrite newer state.

## What it does

A `Snapshot` references a source `ConfigMap` (and optionally one key,
`subPath`). The lifecycle is:

```
Pending ──job created──▶ Running ──result ConfigMap + succeeded Job──▶ Ready
   │                        │
   └── source missing       └── Job exhausted retries / result invalid ──▶ Failed
```

| Phase   | Meaning |
|---------|---------|
| Pending | No worker Job for the current generation yet (e.g. source ConfigMap missing). |
| Running | The deterministic worker Job for the current generation is in flight. |
| Ready   | The Job succeeded and a verified SHA-256 digest is recorded. |
| Failed  | The Job failed for this generation (bad `subPath`, result missing/invalid). |

Every status write sets `status.observedGeneration = metadata.generation`.

### The digest (real cryptography, not a mock)

The in-Job script mounts the source ConfigMap, hashes each key with
**coreutils `sha256sum`**, writes a deterministic manifest and publishes it as
a result `ConfigMap`. The Go reference implementation
(`internal/digest`) uses **`crypto/sha256`** on the same bytes:

1. Select all keys (or only `subPath`); sort names by byte order.
2. For each file emit one manifest line:
   `<sha256(value)>  <byte-length>  <name>`
3. `digest = sha256( manifest )`, where the manifest is the lines joined with
   `\n` (no trailing newline).

Because the manifest carries each content hash, size and (sorted) name, the
root digest binds **content, names and ordering**. The shell worker and the Go
implementation are cross-checked by a test (`internal/worker`) that runs the
real script against a stub `kubectl` and asserts byte-identical digests.
`cmd/verifier` independently re-fetches the live source and recomputes.

## Correctness guarantees (and where they are tested)

| Guarantee | Mechanism | Test |
|-----------|-----------|------|
| Repeat reconcile never creates a duplicate Job | Job/result names are a deterministic function of `(snapshot, generation)` | `TestHappyPathJobProducesRealDigest`, E2E step 1 |
| Recovery from restart | All state lives in the cluster (Job + result ConfigMap); status is rebuilt from it | `TestRestartRecoveryReadyUnwritten`, `TestRestartMidRunning`, E2E step 4 |
| Job succeeded but status not written | Next reconcile reads the result ConfigMap and writes Ready | `TestRestartRecoveryReadyUnwritten` |
| Old-generation Job completing late cannot overwrite newer state | Results are accepted only when their generation label/payload equals the current generation; older resources are GC'd first | `TestSpecChangeOldGenerationCannotOverwrite`, E2E step 5 |
| Delete waits for dependents | Finalizer deletes owned Jobs/ConfigMaps and is removed only after list is empty | `TestDeletionWaitsForDependents`, E2E step 6 |
| Real file digest | Worker runs `sha256sum` on real mounted bytes; result ConfigMap is read back | `internal/worker` test, `cmd/verifier`, E2E steps 1–2 |
| Deterministic failure | Missing `subPath` makes the Job exit non-zero; failures bind observedGeneration | `TestJobFailure`, E2E step 3 |

## Layout

```
api/v1alpha1/            Snapshot CRD types (+ hand-written deepcopy)
cmd/manager/             controller binary
cmd/verifier/            standalone digest re-computation / verification CLI
internal/controller/     reconciler: jobs, finalizer, generation GC, status
internal/digest/         Go reference digest (crypto/sha256) + verifier
internal/worker/         worker.sh (embedded into the binary) and equivalence tests
config/crd/bases/        CRD manifest
config/manager/          controller Deployment/RBAC/Namespace
config/rbac/             worker ServiceAccount + Role
config/samples/          example Snapshot + source ConfigMap
scripts/e2e.sh           full kind end-to-end
scripts/devregistry.sh   local registry provisioning for kind
scripts/envtest-assets.sh locate/download envtest etcd+apiserver
Dockerfile               manager image (vendored, offline build)
Dockerfile.worker        worker Job image (debian-slim + kubectl)
kind-cluster.yaml        single-node kind cluster (v1.30.10) + registry config
```

## Prerequisites

- Go **1.22+**
- Docker, [`kind`](https://kind.sigs.k8s.io) v0.23+, `kubectl`
- The E2E worker runs from a small image (`Dockerfile.worker`: `debian:bookworm-slim`
  + `kubectl`, provides `bash`/`coreutils`/`findutils`) which is built and
  pushed to a **local Docker registry** at `localhost:5000`. This avoids
  `kind load`, which fails on hosts whose Docker uses the containerd image
  store (`failed to detect containerd snapshotter`). The first `make e2e`
  starts that registry, wires it into the cluster, and builds both images
  offline (Go deps are vendored).

If `kind`/`kubectl` are not on `PATH`, e.g.:

```bash
mkdir -p ~/.local/bin
curl -sSL -o ~/.local/bin/kind     https://kind.sigs.k8s.io/dl/v0.23.0/kind-linux-amd64
curl -sSL -o ~/.local/bin/kubectl  https://dl.k8s.io/release/v1.30.10/bin/linux/amd64/kubectl
chmod +x ~/.local/bin/kind ~/.local/bin/kubectl
export PATH="$HOME/.local/bin:$PATH"
```

## Quick start (local acceptance)

```bash
# 1. build
make build

# 2. unit tests: digest + shell/Go cross-implementation equivalence
make test

# 3. controller integration tests against a real kube-apiserver/etcd (envtest)
make envtest          # downloads envtest binaries automatically on first run

# 4. full end-to-end on a real kind cluster (build, deploy, 6 scenarios)
make e2e
```

That's the acceptance sequence: **`make test && make envtest && make e2e`**.

### Step-by-step manual run

```bash
make kind-up                 # create cluster + wire in local registry
make deploy                  # build+push both images, apply CRD/RBAC/Deployment
make samples                 # create source ConfigMap + sample Snapshots

kubectl --context kind-snapshot-p081a get snapshots
# NAME                 PHASE     GEN   DIGEST     AGE
# app-snapshot         Ready     1     ab12…      30s
# app-snapshot-subpath Ready     1     …          30s
# pending-demo         Pending   1     …          30s

kubectl --context kind-snapshot-p081a get jobs
kubectl --context kind-snapshot-p081a logs -n snapshot-system deploy/snapshot-controller

# independently verify a digest against the live source ConfigMap
go run ./cmd/verifier --context kind-snapshot-p081a \
  --snapshot app-snapshot --namespace default
```

Watch a generation change (spec edit bumps `metadata.generation`):

```bash
kubectl patch snapshot app-snapshot --type=merge \
  -p '{"spec":{"subPath":"config.yaml"}}'
kubectl get snapshot app-snapshot -w
```

Tear down:

```bash
make undeploy        # remove controller + CRD (deletes snapshots)
make kind-down
```

## How recovery/consistency works

- **Deterministic names:** `snap-<snapshot>-<generation>-<hash>` is derived
  solely from the snapshot name and generation, so a reconcile after restart
  finds the existing Job instead of creating a second one.
- **Generation fence:** the controller only accepts a result whose
  `snapshot.example.com/generation` label (and payload) equals
  `metadata.generation`. On a spec change it first garbage-colletes older
  Jobs/results and requeues, so an old Job that finishes late is ignored.
- **Result object:** the worker writes a labelled `ConfigMap` (same name as
  the Job) via the Kubernetes API. Watching that object wakes the controller
  the moment a digest is available.
- **Finalizer:** on deletion the controller deletes every owned Job and result
  ConfigMap across all generations and only then removes the finalizer, so the
  Snapshot never vanishes while its dependents still exist.
- **Optimistic concurrency:** status updates use the status subresource; a
  conflict causes a fresh requeue rather than a blind overwrite.

## Notes / limitations

- Worker Jobs run `internal/worker/worker.sh` (embedded into the manager and
  passed to the container) inside `Dockerfile.worker`'s image
  (`debian:bookworm-slim` + `kubectl`). In another cluster point
  `--job-image` at any image with `bash`, `coreutils`, `findutils`, `sed`,
  `grep` and a compatible `kubectl`, and grant the `snapshot-worker`
  ServiceAccount the Role in `config/rbac/worker.yaml`.
- Images are served from a local registry rather than loaded with
  `kind load` to support Docker's containerd image store; `make e2e` sets this
  up automatically.
- `subPath` names a ConfigMap key (exposed as a file in the mount).
- This is a backend-only project; no UI is included.
