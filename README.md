# HTTP Range Response Semantics — pure backend (Go)

A self-contained Go service that serves **immutable binary artifacts** with
full HTTP range semantics (RFC 9110 §13 preconditions, §14 range requests),
plus a **fault-injecting client** that fetches, reassembles and verifies the
original bytes. Everything runs in-process on loopback; there is no database,
no disk storage and no production/external dependency.

- Prefix, open-ended and suffix byte ranges
- Single (`206` + `Content-Range`) and multiple (`multipart/byteranges`) ranges
- Correct `416` + `Content-Range: bytes */<size>` for unsatisfiable requests
- `If-Range` with strong ETags **and** HTTP-dates; weak tags ignored per spec
- Ranges are evaluated against the **selected representation** (identity or a
  deterministic gzip encoding), each with its own strong ETag
- Zero-length artifacts, overlap handling, and a hard cap on range members
- Deterministic **structured JSON results** for every acceptance scenario
- A controllable clock (fake clock in tests), no direct `time.Now` in logic

## Layout

```
cmd/server        HTTP service entry point (seeds in-memory artifacts)
cmd/selftest      acceptance harness: starts two in-process origins on real
                  loopback ports, runs scenarios with the fault client,
                  emits selftest-report.json
internal/clock    Clock interface: System and immutable Fake
internal/artifact immutable Artifact (defensive copies), concurrency-safe Store
internal/rangespec  Range header parser, resolver/clamping, Content-Range
internal/mpart    multipart/byteranges writer with exact Content-Length
internal/server   representation selection, validators, HTTP handlers
internal/origin   brings the service up on 127.0.0.1 for server and tests
internal/fault    RoundTripper that truncates / aborts / corrupts / strips ETag
internal/client   range client, multipart parser, reassembly + byte verification
internal/report   immutable structured result types (JSON)
examples/         curl script and .http request samples
```

## Build and run

Requires Go 1.22+. No third-party modules are used (`go.mod` has no requires).

```bash
go build ./...
go run ./cmd/server                 # http://127.0.0.1:8080
# flags: -addr 127.0.0.1:8080  -gzip  -max-ranges 5
```

Seeded artifacts: `hello.txt` (18 bytes), `tiny.bin` (26), `sample.bin`
(1024), `empty.bin` (0). List them at `GET /`.

### Try it

```bash
curl -i -H "Range: bytes=0-4"            http://127.0.0.1:8080/artifacts/hello.txt
curl -i -H "Range: bytes=0-4,12-17"      http://127.0.0.1:8080/artifacts/hello.txt
curl -i -H "Range: bytes=100-"           http://127.0.0.1:8080/artifacts/hello.txt
./examples/requests.sh                  # all samples, annotated
```

## Acceptance tests (actually run)

The harness is an executable, not just a Go test: it starts the real HTTP
service on a real loopback port and drives it over the wire.

```bash
go run ./cmd/selftest                 # writes selftest-report.json
go test ./...                         # package unit tests
go test -race ./...                   # data-race check
go test -cover ./internal/...         # statement coverage
```

Latest recorded result: **28/28 acceptance scenarios pass** (24 range/
precondition scenarios R01–R24 and 4 fault-injection scenarios F01–F04).
Unit-test statement coverage is ≥ 80% in every package; the race detector
is clean. See [`RUNLOG.md`](./RUNLOG.md) for the exact commands and output.

### Coverage required by the brief

| Requirement                        | Where verified                |
|------------------------------------|-------------------------------|
| unsatisfiable ranges               | R07, R08, R10 (unit: resolve) |
| zero-length artifact               | R09 (200), R10 (416 `*/0`)    |
| overlapping ranges                 | R12                           |
| ETag mismatch                      | R16                           |
| client reassembly == original bytes| R11, R02–R06, R22             |
| range count capped                 | R13 (at limit), R14 (over)    |
| truncated / aborted / corrupt body | F01, F02, F03                 |
| ranges on selected representation  | R22 (gzip ETag, encoded bytes)|

## Semantics summary

- **Strong ETag** = quoted SHA-256 over the representation bytes; the gzip
  representation has a different ETag from identity.
- **Clamping** follows RFC 9110 §14.1.1: a `last-pos` past the end is clamped;
  a suffix longer than the resource covers all of it; only when *no* member
  intersects does the server answer `416` (with `Content-Range: bytes */size`).
- **Malformed `Range`** (syntax error or a non-`bytes` unit) is **ignored**,
  and the full representation is returned with `200` — it is never a 4xx.
- **`If-Range`**: a matching strong ETag or a date not earlier than
  `Last-Modified` allows the partial response; a mismatch returns `200` with
  the full representation. A weak `W/"…"` or unparseable field is ignored.
- **Overlaps/duplicates** are preserved as sent. Strict `Reassemble` rejects
  overlaps and gaps; the client separately verifies every received part
  against an independently fetched ground-truth copy, so overlaps are
  delivered but cannot mask a byte error.
- **Range-member cap**: more than `maxRanges` (default 5) members makes the
  server ignore the `Range` header and answer `200`, preventing multipart
  amplification. The limit is advertised via `X-Range-Member-Limit`.
- **Zero-length artifact**: plain GET is `200`/`Content-Length: 0`; any range
  is unsatisfiable (`416`, `Content-Range: bytes */0`).

## Design notes

- **Immutability:** artifacts never change after construction; `Bytes()` and
  `Slice()` return copies, and the fake clock's `Advance` returns a new clock
  rather than mutating the receiver. Reassembly builds a fresh buffer.
- **Selected representation:** content negotiation happens before range
  resolution. Range offsets and every `Content-Range` refer to the bytes
  actually delivered (gzip carries `Content-Encoding` + `Vary: Accept-Encoding`).
- **Fault injection** is a decorating `http.RoundTripper`; it only affects the
  client side, so a correct origin and a hostile transport coexist in one
  process.
