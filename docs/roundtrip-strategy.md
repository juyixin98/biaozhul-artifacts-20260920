# Field round-trip & data-retention strategy

This document pins down exactly what happens to every field when a `Task`
crosses the v1alpha1 ↔ v1 boundary, and why. The implementation lives in
`internal/conversion/convert.go`; this file is the contract it implements.

## 1. Field map

| Field                         | v1alpha1                       | v1                                   | Conversion policy |
|-------------------------------|--------------------------------|--------------------------------------|-------------------|
| Whole-second timeout          | `spec.timeoutSeconds` (int64)  | `spec.timeout.seconds` (int64)       | Mapped 1:1.       |
| Sub-second offset             | —                              | `spec.timeout.nanos` (int32, 0..1e9) | **Not expressible in v1alpha1 — explicit error, never truncated.** |
| New scheduling class          | —                              | `spec.priority` (Normal/High/Low)    | Preserved annotation (see §3). |
| New free-form tags            | —                              | `spec.tags[]`                        | Preserved annotation (see §3). |
| `spec.payload` / status fields | identical                     | identical                            | Copied verbatim.  |
| Other unknown `spec` keys     | any                            | any                                  | Preserved by schema + converter (see §4). |
| `metadata.labels/annotations` | any                            | any                                  | Preserved by API machinery. |

## 2. Inexpressible values fail loudly

v1α1 only knows whole seconds. A v1 object with `timeout.nanos != 0` cannot be
served as v1alpha1 without losing information. The converter therefore
**rejects** that down-conversion:

```
InexpressibleValue: spec.timeout has sub-second component 500000000ns but
v1alpha1 only supports whole seconds; remove the nanoseconds before serving
this object to old clients
```

There is deliberately no rounding, no flooring and no "best effort". The same
holds for structurally invalid durations (`seconds` missing/negative,
`nanos` outside `[0, 999999999]`) and for a corrupt preserve annotation.

Consequence: an object that genuinely needs sub-second precision cannot be
read by old clients. That is the correct trade-off — an old client acting on a
silently truncated timeout (2.5s → 2s) is worse than a visible error that
makes the incompatibility explicit. New clients are unaffected: creating and
reading the object as v1 works normally.

> Note on admission: Kubernetes invokes configured admission webhooks while
> building multiple review versions of a request. With `matchPolicy: Exact`
> (see §5) a *create* of an invalid v1 object is rejected by the validating
> webhook; in some code paths the API server surfaces the conversion error
> instead. Both are failures with the underlying reason attached — clients
> must treat the request as refused, not stored.

## 3. New fields: preserve-on-downgrade, restore-on-upgrade

v1-only fields cannot be deleted just because an old client is looking at the
object. The storage version is v1; etcd must keep `priority`/`tags` even while
a v1alpha1 client has the object open.

Mechanism — a single reserved annotation owned by the conversion webhook:

```
migration.example.io/v1-spec-preserve:
  {"apiVersion":"migration.example.io/v1","kind":"Task",
   "spec":{"priority":"High","tags":["backup","nightly"]}}
```

* **v1 → v1alpha1**: `priority`/`tags` are removed from `spec` and serialized
  into the annotation. `timeout.seconds` becomes `timeoutSeconds`.
* **v1alpha1 → v1**: the annotation is decoded, its `spec` keys are restored
  and the annotation itself is removed (it never leaks into the v1 view).

So the legacy client's flow — `GET v1alpha1` → change `payload` →
`PUT v1alpha1` — carries the annotation round-trip untouched, and the stored
v1 object keeps `priority`/`tags`. This is verified end-to-end by
`TestOldClientUpdateDoesNotEraseV1Fields`.

Only the whitelisted v1-only keys (`priority`, `tags`) are parked there.
Structural validity of the annotation is checked on decode; a malformed value
fails with `MalformedPreserveAnnotation` rather than being discarded.

Defaults are never synthesized by conversion. `spec.priority: Normal` shown to
a v1 reader comes from the **CRD structural schema** (`default: "Normal"`),
which the API server applies when serving the v1 representation — not from the
conversion webhook. The mutating admission webhook is the other default source
on create/update for both versions (`timeout(Seconds)` default 30).

## 4. Unknown fields: schema-declared retention

The requirement "whether unknown fields are retained" is answered
**explicitly in the CRD schema**, not left to the default:

* `spec.preserveUnknownFields: false` at the CRD root (structural schema).
* `spec.x-kubernetes-preserve-unknown-fields: true` on `spec` and `status`
  **in both versions** (`config/crd/tasks.migration.example.io.yaml`).

Meaning: keys that neither version models (e.g. an experimental
`spec.experimentalFlux`) are kept through conversion and storage. The
converter additionally operates on unstructured JSON maps rather than on typed
Go structs, so it never re-marshals through a struct that would prune unknown
keys — preservation does not rely solely on the schema flag.

Accepted trade-off: with preservation on, typoed field names are not rejected
by schema validation. Tighten a specific sub-tree by dropping the flag there
once its schema stabilizes.

## 5. Admission `matchPolicy: Exact`

Each mutating/validating webhook entry targets exactly one version
(`apiVersions: ["v1"]` or `["v1alpha1"]`) and sets `matchPolicy: Exact`.

The default `Equivalent` policy would fire a v1alpha1 webhook for a v1 request
after serving the object to the webhook in its v1alpha1 form. That would run
the lossy down-conversion on **every v1 write** and reject perfectly valid
sub-second v1 durations at admission time. `Exact` keeps version-specific
defaulting/validation on its own version; cross-version conversion only
happens for storage/serving, where this contract applies.

## 6. Timeouts

* The webhook enforces a per-object deadline (`--conversion-timeout`, default
  10s) and fails with `ConversionCancelled` when the context expires; a
  partial object is never returned.
* The API server also bounds conversion calls. Its hard timeout for a **CRD
  conversion** webhook is 30s (non-configurable; admission webhooks default to
  10s via `timeoutSeconds`). `TestConversionTimeoutEndToEnd` (run with
  `RUN_SLOW_TESTS=1`) proves a conversion that blocks 45s is aborted by the
  API server at ~30s with a webhook/timeout error.
