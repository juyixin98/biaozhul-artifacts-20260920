# Run log — commands and observed results

Environment: Linux 6.8.0-90-generic, `go version go1.22.2 linux/amd64`.
All commands run from the repository root. No network/production system is
involved; the service listens on `127.0.0.1` and every dependency is an
in-process fake.

## 1. Static checks

```text
$ go vet ./...
vet: OK (no output)

$ go build ./...
build: OK
```

## 2. Unit tests

```text
$ go test ./...
?   httprange/cmd/selftest            [no test files]
?   httprange/cmd/server              [no test files]
ok  httprange/internal/artifact
ok  httprange/internal/client
ok  httprange/internal/clock
ok  httprange/internal/fault
ok  httprange/internal/mpart
ok  httprange/internal/origin
ok  httprange/internal/rangespec
ok  httprange/internal/report
ok  httprange/internal/server
```

79 test functions/variants across the packages; all pass.

## 3. Coverage (≥ 80% in every package)

```text
$ go test -cover ./internal/...
ok  httprange/internal/artifact   coverage: 94.4%
ok  httprange/internal/client     coverage: 81.4%
ok  httprange/internal/clock      coverage: 100.0%
ok  httprange/internal/fault      coverage: 87.0%
ok  httprange/internal/mpart      coverage: 82.8%
ok  httprange/internal/origin     coverage: 92.3%
ok  httprange/internal/rangespec  coverage: 98.7%
ok  httprange/internal/report     coverage: 100.0%
ok  httprange/internal/server     coverage: 80.9%
```

(The network methods of `client` are exercised end-to-end by the acceptance
harness in `cmd/selftest`, which is not counted in package coverage.)

## 4. Race detector

```text
$ go test -race ./...
ok  httprange/internal/artifact
ok  httprange/internal/client
ok  httprange/internal/clock
ok  httprange/internal/fault
ok  httprange/internal/mpart
ok  httprange/internal/origin
ok  httprange/internal/rangespec
ok  httprange/internal/report
ok  httprange/internal/server
```
Clean — no data races.

## 5. Acceptance harness over real loopback HTTP

```text
$ go run ./cmd/selftest
[PASS] R01  200  full representation baseline
[PASS] R02  206  prefix range
[PASS] R03  206  open-ended range
[PASS] R04  206  suffix range
[PASS] R05  206  suffix longer than representation covers all
[PASS] R06  206  last-pos beyond end is clamped
[PASS] R07  416  unsatisfiable: first-pos at size
[PASS] R08  416  unsatisfiable: entirely past end
[PASS] R09  200  zero-length artifact: plain GET
[PASS] R10  416  zero-length artifact: any range is unsatisfiable
[PASS] R11  206  multiple ranges: exact partition reassembles to original
[PASS] R12  206  overlapping ranges delivered; strict reassembly rejects overlap
[PASS] R13  206  range count at the limit is served
[PASS] R14  200  range count over the limit is ignored (200 full)
[PASS] R15  206  If-Range matching strong ETag allows 206
[PASS] R16  200  If-Range mismatched ETag returns full 200
[PASS] R17  206  If-Range weak ETag is ignored (206)
[PASS] R18  206  If-Range date newer than Last-Modified allows 206
[PASS] R19  200  If-Range stale date returns full 200
[PASS] R20  200  malformed Range header is ignored
[PASS] R21  200  unsupported range unit is ignored
[PASS] R22  206  ranges index the selected (gzip) representation
[PASS] R23  304  If-None-Match hit yields 304
[PASS] R24  206  HEAD returns range metadata without a body
[PASS] F01  200  fault: truncated body is detected
[PASS] F02  200  fault: abrupt abort is detected
[PASS] F03  206  fault: corrupted byte fails end-to-end verification
[PASS] F04  206  fault: ETag stripped does not fool byte verification

TOTAL 28  PASS 28  FAIL 0  SKIP 0
```

Structured per-case results (request, selected response headers,
content length, body SHA-256, status, elapsed ms) are written to
[`selftest-report.json`](./selftest-report.json).

## 6. Live curl spot checks against the running binary

`go build -o server ./cmd/server && ./server -addr 127.0.0.1:18080`

```text
$ curl -i -H "Range: bytes=0-4" .../artifacts/hello.txt
HTTP/1.1 206 Partial Content
Content-Range: bytes 0-4/18
Content-Length: 5
Etag: "9ef630ab5208825b17e9b23d6594041f"
Accept-Ranges: bytes

hello

$ curl -s -H "Range: bytes=0-4,12-17" .../artifacts/hello.txt
--httprange-a1b97ed8f71510eae202fac30a49a3
Content-Type: text/plain; charset=utf-8
Content-Range: bytes 0-4/18

hello
--httprange-a1b97ed8f71510eae202fac30a49a3
Content-Type: text/plain; charset=utf-8
Content-Range: bytes 12-17/18

world
--httprange-a1b97ed8f71510eae202fac30a49a3--

$ curl -s -o /dev/null -w "%{http_code}\n" -H "Range: bytes=100-" .../hello.txt
416                                          # Content-Range: bytes */18

$ curl -s -o /dev/null -w "%{http_code}\n" -H "Range: bytes=0-0" .../empty.bin
416                                          # Content-Range: bytes */0
```

gzip selected representation (`./server -gzip`, request `Accept-Encoding: gzip`):

```text
GET full      -> 200 Content-Encoding: gzip, ETag "27cb348d…", 50 bytes
GET bytes=0-  -> 206 Content-Range: bytes 0-49/50, Content-Encoding: gzip
cmp(full-gzip-body, range-0--body)  -> identical bytes
identity ETag "9e969ed8…" != gzip ETag "27cb348d…"
```

## 7. Failures encountered and how they were resolved

The suite did **not** pass on the first run; this is recorded honestly:

- **R19 initially failed.** `If-Range` carrying a stale HTTP-date was expected
  to force `200`, but the server answered `206`:

  ```text
  [FAIL] R19  0  If-Range stale date returns full 200
          reason: status 206
  ```

  Root cause: the test helper formatted dates with Go's `time.RFC1123`, which
  renders the UTC zone as the literal `UTC`, while `http.ParseTime` only
  accepts `GMT` for that layout (the IMF-fixdate, RFC 9110 §5.6.7). The
  unparsable date made the server correctly *ignore* the malformed
  `If-Range` and serve `206`. The service behaved per spec; the test fixture
  was wrong. Fixed by formatting with `http.TimeFormat` (`… GMT`). After the
  fix R19 passes and the remaining 27 scenarios were unaffected.

- Two transient tooling issues during development (not product defects):
  one compile error from an unused import/variable, and a gzip spot-check that
  bound a port (`18081`) already held by an unrelated process on the host, so
  curl reached that other service (`404`). Restarting on a free port
  (`19090`) produced the expected result above.

## 8. Known limitations / non-goals

- Backend only; there is deliberately no UI/frontend.
- Artifacts are seeded in memory and exist only for the process lifetime.
- Only `identity` and `gzip` representations are negotiated (no brotli, etc.).
- The service is intended for local loopback use and performs no
  authentication.
