# Resumable Snapshot Controller

A Kubernetes controller that produces **real, digest-verified snapshots** of
local test resources (ConfigMap/Secret) on a [kind](https://kind.sigs.k8s.io/)
cluster. Pure backend: CRD + controller + a worker Job; no UI.

The snapshot is not a status flag — a real worker Pod reads the source payload
through the Kubernetes API, materializes every key to a byte-exact file, packs
a real `tar`/`tar.gz` archive, and computes the real **SHA-256** digest of the
archive and of every file. The controller only flips `Ready` after verifying
the artifact published by that Job.

## Guarantees

- **State machine** `Pending → Running → Ready | Failed`, every status write
  bound to `.metadata.generation` via `status.observedGeneration`.
- **Real artifact, never faked.** Digest/size/files come exclusively from the
  worker's `result.json`, which the controller structurally validates
  (32-byte hex SHA-256, positive size, per-file digests, generation match).
  A complete Job with no artifact becomes `Failed(ResultMissing)` after a
  grace period — it never hangs and never invents a digest.
- **Idempotent Job creation.** The Job for a generation has a deterministic
  name `snap-<snapshot>-g<generation>`. Repeated reconciles create at most one
  Job per generation.
- **Finalizer cleanup.** Deletion blocks on
  `snapshot.example.com/cleanup` until owned Jobs and result ConfigMaps are
  actually gone (listed, not assumed).
- **Stale-generation guard.** Owned objects carry a generation label; a late
  result from an old generation's Job is rejected and garbage-collected and can
  never overwrite the newer generation's status.
- **Resumable.** The controller rebuilds all decisions from cluster state on
  (re)start; a Job that completes while the controller is down still converges
  to `Ready` once the controller is back.

## Layout

```
api/v1alpha1/             Snapshot CRD types (+ generated deepcopy)
cmd/manager/              controller manager entrypoint
internal/controller/      reconciler, naming, event mapping, envtest suite
worker/                   real worker: shell + curl/jq/tar/sha256sum, Dockerfile
config/crd/bases/         generated CRD
config/rbac/              generated manager ClusterRole + per-ns worker Role template
config/manager/           namespace, manager RBAC, Deployment
config/samples/demo.yaml  example source ConfigMap/Secret + Snapshot
hack/e2e.sh               full local kind end-to-end (all scenarios)
hack/kind-config.yaml     kind node pinned to v1.31.0
```

## Requirements

- Go 1.22+
- Docker
- `make` (downloads `kind` v0.24, `kubectl` v1.31, `controller-gen`,
  `setup-envtest` into `./bin`)
- Outbound network on first run (Go modules, kind/kubectl binaries,
  `kindest/node` image). Container builds behind a proxy inherit
  `HTTP(S)_PROXY` via `--network=host` in the e2e script.

## Quick start (local kind)

```bash
make e2e          # builds images, creates cluster, runs every scenario
make e2e-down     # deletes the kind cluster
```

Manual step-by-step equivalent:

```bash
make tools generate build
make docker-build
bin/kind create cluster --name snapshot-e2e --config hack/kind-config.yaml \
  --kubeconfig bin/kubeconfig-e2e
export KUBECONFIG=$PWD/bin/kubeconfig-e2e
bin/kind load docker-image snapshot-controller:dev snapshot-worker:dev \
  --name snapshot-e2e

bin/kubectl apply -f config/crd/bases/snapshot.example.com_snapshots.yaml
bin/kubectl apply -f config/manager/namespace.yaml
bin/kubectl apply -f config/rbac/role.yaml
bin/kubectl apply -f config/manager/rbac.yaml
bin/kubectl apply -f config/manager/deployment.yaml

bin/kubectl create namespace snapshot-demo
sed 's/PLACEHOLDER_NAMESPACE/snapshot-demo/g' config/rbac/worker-role.yaml.tmpl \
  | bin/kubectl apply -f -
bin/kubectl apply -f config/samples/demo.yaml

bin/kubectl -n snapshot-demo get snapshots
bin/kubectl -n snapshot-demo get snapshot demo -o yaml
```

## Acceptance checks

### Automated envtest (no cluster needed)

```bash
make test
```

Spins up a real kube-apiserver/etcd via `envtest` and runs, among others:

| Test | What it proves |
|------|----------------|
| `TestHappyFlow` | Pending→Running→Ready, digest + `observedGeneration=1` |
| `TestNoDuplicateJobs` | many reconciles ⇒ exactly one Job |
| `TestJobFailure` | backoff-exhausted Job ⇒ `Failed/JobFailed`, stable |
| `TestJobSucceedsButStatusNotWrittenThenRestart` | complete Job + artifact while controller is down ⇒ `Ready` after restart |
| `TestSuccessfulJobWithoutArtifact` | complete Job, no result ⇒ `Failed/ResultMissing` after grace |
| `TestInvalidResultFails` | malformed artifact ⇒ `Failed/ResultInvalid` (no fake Ready) |
| `TestSpecChangeStaleJobGuard` | spec bump ⇒ old Job/result GC'd; late gen-1 result cannot win |
| `TestDeletionWaitsForDependents` | finalizer held until dependents actually deleted |
| `TestDeletionRaceJobRecreatedDuringDeletion` | an owned object appearing mid-termination is also awaited |

`make test` runs with `-race`.

### Local end-to-end on real kind

```bash
bash hack/e2e.sh
```

Runs a real controller Deployment and the real worker image and asserts:

1. Happy flow: `Ready`, then the script **independently re-materializes the
   ConfigMap locally with `sha256sum`** and compares every per-file digest to
   the status/`result.json` — real bytes, not a status echo.
2. Repeated `annotate` reconciles leave exactly one Job.
3. `rollout restart` of the controller mid-flight still converges to `Ready`.
4. A spec change bumps to generation 2 with a new digest; generation-1 Job and
   result ConfigMap are garbage collected; a forged late generation-1 result
   is rejected and removed without disturbing generation 2.
5. Deletion observes the finalizer waiting window and finishes only after all
   Jobs/result ConfigMaps are gone.

### Inspect a live object

```bash
bin/kubectl -n snapshot-demo get snap demo
# PHASE   OBSERVED GEN   SHA256                                                          AGE
# Ready   2              e3b0…                                                           5m

bin/kubectl -n snapshot-demo get cm snap-result-demo-g2 -o jsonpath='{.data.result\.json}' | jq
```

## How the worker works

The Job Pod (`snapshot-worker:dev`, non-root) runs `worker/worker.sh`:

1. Authenticates to the API with its projected service-account token over HTTPS.
2. GETs the source ConfigMap/Secret and the owner Snapshot (for its UID).
3. Writes each key to `/work/src/<key>` (ConfigMap string values are
   base64-decoded from `@base64`; Secret `data` is passed through), guarding
   against path traversal.
4. `tar` packs `/work/src` (gzip for `.gz` names).
5. `sha256sum` computes the archive and per-file digests over the real bytes.
6. POSTs a result ConfigMap (PUT with the live `resourceVersion` on conflict)
   containing `result.json`, labeled with the generation and owned by the
   Snapshot.

RBAC is least-privilege: the manager gets a ClusterRole scoped to Snapshots,
Jobs, ConfigMaps and read-only Secrets; workers get a per-namespace Role that
can read sources and publish ConfigMaps only.

## Regenerating after API changes

```bash
make generate     # deepcopy, CRD, RBAC
```

## Notes on honesty / scope

- This operates only on **local kind test resources**. It does not snapshot
  volumes or cloud storage and does not push artifacts anywhere; the digest
  proves a real local archive was produced.
- All cryptographic operations (SHA-256) and all Kubernetes protocol
  operations are executed for real in tests and e2e; no digest is synthesized.
- Failures (Job failure, missing/invalid artifact, stale generation) are
  reported as explicit `Failed` phases with machine-readable reasons, not
  swallowed.
