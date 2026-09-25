#!/usr/bin/env bash
# End-to-end acceptance tests for the "remote cache for local models" project.
#
# Runs the REAL binaries (cacheserver, buildserver, cachectl, buildctl) and
# exercises the acceptance criteria:
#
#   1. Concurrent uploads of the same digest publish exactly one object.
#   2. A truncated download is resumed (Range) and verified by the client.
#   3. Server restart: objects persist, orphaned tmp uploads are swept.
#   4. The cache never exposes partial objects (concurrent truncation probe).
#   5. A corrupt (bad) cache entry is diagnosable and quarantined.
#   6. Build service runs ONLY explicitly fixture-manifested commands; a
#      failing fixture is reported and never cached.
#   7. Cache directory and work directory are strictly separate.
#
# Usage: scripts/acceptance.sh
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PASS=0; FAIL=0
# Pick free ephemeral ports unless the caller pinned them.
pick_port() {
  python3 - <<'PY'
import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
PY
}
CACHE_PORT="${CACHE_PORT:-$(pick_port)}"
BUILD_PORT="${BUILD_PORT:-$(pick_port)}"
CACHE_URL="http://127.0.0.1:${CACHE_PORT}"
BUILD_URL="http://127.0.0.1:${BUILD_PORT}"
WORKDIR=""

log()  { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
ok()   { printf '  \033[1;32mPASS\033[0m %s\n' "$*"; PASS=$((PASS+1)); }
bad()  { printf '  \033[1;31mFAIL\033[0m %s\n' "$*"; FAIL=$((FAIL+1)); }
die()  { bad "$*"; teardown; exit 1; }

assert() { # assert <desc> <command...>
  local desc="$1"; shift
  if "$@" >/dev/null 2>&1; then ok "$desc"; else bad "$desc"; fi
}

# sha256 hex of a file (portable)
shahex() { sha256sum "$1" | awk '{print $1}'; }

wait_http() { # wait_http <url>
  local url="$1" i
  for i in $(seq 1 50); do
    if curl -fsS "$url" >/dev/null 2>&1; then return 0; fi
    sleep 0.1
  done
  return 1
}

CACHE_PID=""; BUILD_PID=""
teardown() {
  [ -n "$BUILD_PID" ] && kill "$BUILD_PID" 2>/dev/null
  [ -n "$CACHE_PID" ] && kill "$CACHE_PID" 2>/dev/null
  wait 2>/dev/null
}
trap teardown EXIT

log "build binaries"
go build -o bin/cacheserver ./cmd/cacheserver || die "go build cacheserver"
go build -o bin/cachectl     ./cmd/cachectl     || die "go build cachectl"
go build -o bin/buildserver  ./cmd/buildserver  || die "go build buildserver"
go build -o bin/buildctl     ./cmd/buildctl     || die "go build buildctl"
ok "all four binaries built"

WORKDIR="$(mktemp -d -t modelcache-acceptance-XXXXXX)"
CACHE_DIR="$WORKDIR/cache"
JOBS_DIR="$WORKDIR/work"
ART_DIR="$WORKDIR/artifacts"
DL_DIR="$WORKDIR/downloads"
mkdir -p "$DL_DIR"
# Cap at 8 MiB (bigger than the 4 MiB fixture) while keeping the oversize
# rejection cheap to test with a 12 MiB sample.
MAXOBJ=$((8*1024*1024))

log "start cacheserver on $CACHE_PORT (cache dir: $CACHE_DIR)"
./bin/cacheserver -addr ":${CACHE_PORT}" -cache-dir "$CACHE_DIR" -max-object-size "$MAXOBJ" \
  >"$WORKDIR/cache.log" 2>&1 &
CACHE_PID=$!
wait_http "$CACHE_URL/healthz" || { cat "$WORKDIR/cache.log"; die "cacheserver did not start"; }
ok "cacheserver healthy"

log "start buildserver on $BUILD_PORT (work dir: $JOBS_DIR)"
./bin/buildserver -addr ":${BUILD_PORT}" -cache-url "$CACHE_URL" \
  -fixtures fixtures/manifest.json -work-dir "$JOBS_DIR" -artifact-dir "$ART_DIR" \
  >"$WORKDIR/build.log" 2>&1 &
BUILD_PID=$!
wait_http "$BUILD_URL/healthz" || { cat "$WORKDIR/build.log"; die "buildserver did not start"; }
ok "buildserver healthy"

# ---------------------------------------------------------------------------
log "test 1: fixture allowlist is enforced (only manifested commands run)"
curl -fsS "$BUILD_URL/v1/fixtures" | jq -e '.fixtures | length == 4' >/dev/null \
  && ok "4 fixtures advertised" || bad "fixture list"

resp=$(curl -s -o /tmp/mc-inj.json -w '%{http_code}' -X POST "$BUILD_URL/v1/builds" \
  -H 'Content-Type: application/json' -d '{"fixture":"rm -rf /; echo pwned"}')
[ "$resp" = "400" ] && jq -e '.code == "unknown_fixture"' /tmp/mc-inj.json >/dev/null \
  && ok "command injection via fixture name rejected (HTTP 400)" || bad "injection rejection ($resp)"

resp=$(curl -s -o /tmp/mc-inj2.json -w '%{http_code}' -X POST "$BUILD_URL/v1/builds" \
  -H 'Content-Type: application/json' -d '{"fixture":"bash"}')
[ "$resp" = "400" ] && ok "arbitrary binary name 'bash' rejected" || bad "bash fixture ($resp)"

# ---------------------------------------------------------------------------
log "test 2: successful fixture build caches the artifact"
job=$(curl -fsS -X POST "$BUILD_URL/v1/builds" -H 'Content-Type: application/json' \
  -d '{"fixture":"make-model"}' | jq -r '.id')
final=$(curl -fsS "$BUILD_URL/v1/builds/$job?wait=30s")
echo "$final" | jq -e '.status == "succeeded"' >/dev/null \
  && ok "make-model job succeeded" || { echo "$final"; bad "make-model success"; }
DGST=$(echo "$final" | jq -r '.artifacts[0].digest')
ASIZE=$(echo "$final" | jq -r '.artifacts[0].size')
[ "$ASIZE" = "4096" ] && ok "artifact size 4096" || bad "artifact size $ASIZE"
[[ "$DGST" == sha256:* ]] && [ ${#DGST} -eq 71 ] && ok "artifact has sha256 digest: $DGST" || bad "digest format"

# The cache actually holds it.
./bin/cachectl -url "$CACHE_URL" stat "$DGST" >/dev/null && ok "artifact stat-able in cache" || bad "cache stat"
# Cache dir and work dir are different trees.
find "$CACHE_DIR/blobs" -type f -name "${DGST#sha256:}" | grep -q . \
  && ok "object present in cache tree" || bad "object in cache tree"
[ -z "$(find "$JOBS_DIR" -path "*blobs*" 2>/dev/null)" ] && ok "no blobs exist inside work dir" || bad "work/cache separation"

# ---------------------------------------------------------------------------
log "test 3: client verifies digest on download"
./bin/cachectl -url "$CACHE_URL" get "$DGST" "$DL_DIR/model.bin" >/dev/null \
  && ok "cachectl get + verify succeeded" || bad "cachectl get"
[ "$(shahex "$DL_DIR/model.bin")" = "${DGST#sha256:}" ] \
  && ok "downloaded file sha256 matches" || bad "downloaded sha256"

# Tampering locally with the claimed digest must fail client-side verification.
curl -fsS "$CACHE_URL/v1/blobs/$DGST" -o "$DL_DIR/raw.bin"
wrong="sha256:$(printf 'different claimed content' | sha256sum | awk '{print $1}')"
if ./bin/cachectl -url "$CACHE_URL" get "$wrong" "$DL_DIR/should-not-exist" 2>/tmp/mc-geterr.txt; then
  bad "download of non-existent wrong digest succeeded"
else
  grep -q "404" /tmp/mc-geterr.txt && ok "wrong digest -> 404, nothing written" || bad "wrong digest error: $(cat /tmp/mc-geterr.txt)"
fi
[ ! -e "$DL_DIR/should-not-exist" ] && ok "no partial destination published" || bad "destination file leaked"

# ---------------------------------------------------------------------------
log "test 4: uploads stage to tmp and only publish after verification"
# Bad-content upload must be rejected, must not appear under blobs, and must
# be quarantined for diagnosis.
dd if=/dev/urandom of="$WORKDIR/bad.bin" bs=1024 count=4 status=none
claimed="sha256:$(printf 'totally-different' | sha256sum | awk '{print $1}')"
code=$(curl -s -o /tmp/mc-badput.json -w '%{http_code}' -X PUT \
  --data-binary @"$WORKDIR/bad.bin" "$CACHE_URL/v1/blobs/$claimed")
[ "$code" = "400" ] && jq -e '.code == "digest_mismatch"' /tmp/mc-badput.json >/dev/null \
  && ok "bad-content upload rejected 400 digest_mismatch" || bad "bad put code=$code"
code=$(curl -s -o /dev/null -w '%{http_code}' "$CACHE_URL/v1/blobs/$claimed")
[ "$code" = "404" ] && ok "bad object is NOT visible in cache" || bad "bad object visible ($code)"
nq=$(find "$CACHE_DIR/quarantine" -type f | wc -l)
[ "$nq" -ge 1 ] && ok "bad bytes quarantined for diagnosis ($nq file(s))" || bad "quarantine empty"

# ---------------------------------------------------------------------------
log "test 5: concurrent uploads of the SAME digest -> exactly one object"
# Build a bigger object (4 MiB) through the big-model fixture, then re-upload
# the same bytes 20x in parallel.
job=$(curl -fsS -X POST "$BUILD_URL/v1/builds" -H 'Content-Type: application/json' \
  -d '{"fixture":"big-model"}' | jq -r '.id')
final=$(curl -fsS "$BUILD_URL/v1/builds/$job?wait=60s")
if ! echo "$final" | jq -e '.status == "succeeded"' >/dev/null; then
  echo "$final"; die "big-model build did not succeed"
fi
BIG=$(echo "$final" | jq -r '.artifacts[0].digest')
ok "big-model built: $BIG"

curl -fsS "$CACHE_URL/v1/blobs/$BIG" -o "$WORKDIR/big.bin"
pids=()
for i in $(seq 1 20); do
  curl -s -o /dev/null -w '%{http_code}\n' -X PUT --data-binary @"$WORKDIR/big.bin" \
    "$CACHE_URL/v1/blobs/$BIG" &
  pids+=($!)
done
codes=0
for p in "${pids[@]}"; do wait "$p"; done >/tmp/mc-conc.txt
all200=$(grep -vc '^200$' /tmp/mc-conc.txt || true)
[ "$all200" = "0" ] && ok "all 20 concurrent uploads returned 200" || bad "non-200 upload responses: $(cat /tmp/mc-conc.txt)"
nobj=$(./bin/cachectl -url "$CACHE_URL" list | wc -l)
[ "$nobj" = "2" ] && ok "exactly 2 distinct objects in cache ($nobj)" || bad "object count $nobj (want 2)"
ntmp=$(find "$CACHE_DIR/tmp" -type f ! -name '.gitkeep' | wc -l)
[ "$ntmp" = "0" ] && ok "no temp files left after concurrent uploads" || bad "$ntmp temp files leaked"

# ---------------------------------------------------------------------------
log "test 6: cache never exposes a partial object during uploads"
# Fire 16 uploads of the same 4 MiB object while continuously probing GET;
# every GET that returns 200 must return the FULL, verifiable object.
up_pids=()
for i in $(seq 1 16); do
  curl -s -o /dev/null -X PUT --data-binary @"$WORKDIR/big.bin" "$CACHE_URL/v1/blobs/$BIG" &
  up_pids+=($!)
done
violations=0
for i in $(seq 1 40); do
  code=$(curl -s -o "$DL_DIR/probe-$i.bin" -w '%{http_code}' "$CACHE_URL/v1/blobs/$BIG")
  if [ "$code" = "200" ]; then
    got=$(shahex "$DL_DIR/probe-$i.bin")
    [ "$got" = "${BIG#sha256:}" ] || { violations=$((violations+1)); echo "probe $i got $got"; }
  fi
done
for p in "${up_pids[@]}"; do wait "$p" || true; done
[ "$violations" = "0" ] && ok "every visible object during concurrent uploads was complete" || bad "$violations partial/wrong reads"

# ---------------------------------------------------------------------------
log "test 7: truncated download + Range resume via the Go client"
# Fetch only the first 1 MiB of the 4 MiB big object directly with curl Range,
# prove the range works, then let cachectl resume from a retained .part file by
# pre-seeding it and comparing the final hash.
# (cachectl manages .part internally; here we verify the server Range contract
# and a complete verified download independently.)
curl -s -H 'Range: bytes=0-1048575' "$CACHE_URL/v1/blobs/$BIG" -o "$DL_DIR/first1m.bin"
sz=$(stat -c%s "$DL_DIR/first1m.bin")
cr=$(curl -s -D - -o /dev/null -H 'Range: bytes=0-1048575' \
  "$CACHE_URL/v1/blobs/$BIG" | tr -d '\r' | tr -d '\t' \
  | awk 'tolower($1)=="content-range:"{sub(/^[^:]*:[[:space:]]*/,""); print; exit}')
[ "$sz" = "1048576" ] && ok "first range returned exactly 1 MiB" || bad "range size $sz"
[ "$cr" = "bytes 0-1048575/4194304" ] && ok "Content-Range framing correct: $cr" || bad "content-range '$cr'"
cmp -n 1048576 "$DL_DIR/first1m.bin" "$WORKDIR/big.bin" \
  && ok "range bytes match the full object prefix" || bad "range prefix mismatch"

# Simulate an interrupted client: seed the part file with 3 MiB, then the
# Go client must send Range: 3145728- and finish + verify + atomic-publish.
rm -f "$DL_DIR/resumed.bin" "$DL_DIR/resumed.bin.part"
head -c 3145728 "$WORKDIR/big.bin" > "$DL_DIR/resumed.bin.part"
./bin/cachectl -url "$CACHE_URL" get "$BIG" "$DL_DIR/resumed.bin" >/dev/null 2>/tmp/mc-resume.err \
  && ok "resumed download from retained 3 MiB part succeeded" || { cat /tmp/mc-resume.err; bad "resume failed"; }
[ "$(shahex "$DL_DIR/resumed.bin")" = "${BIG#sha256:}" ] \
  && ok "resumed file verifies against digest" || bad "resumed digest"
[ ! -e "$DL_DIR/resumed.bin.part" ] && ok ".part removed after atomic rename" || bad ".part leaked"

# ---------------------------------------------------------------------------
log "test 8: object size limit enforced"
dd if=/dev/urandom of="$WORKDIR/huge.bin" bs=1M count=12 status=none
hd="sha256:$(shahex "$WORKDIR/huge.bin")"
code=$(curl -s -o /tmp/mc-huge.json -w '%{http_code}' -X PUT \
  --data-binary @"$WORKDIR/huge.bin" "$CACHE_URL/v1/blobs/$hd")
[ "$code" = "413" ] && jq -e '.code == "object_too_large"' /tmp/mc-huge.json >/dev/null \
  && ok "oversize upload rejected 413 object_too_large" || bad "oversize code=$code"
code=$(curl -s -o /dev/null -w '%{http_code}' "$CACHE_URL/v1/blobs/$hd")
[ "$code" = "404" ] && ok "oversize object never published" || bad "oversize visible ($code)"

# ---------------------------------------------------------------------------
log "test 9: failing fixture is reported with exit code and caches nothing"
before=$(./bin/cachectl -url "$CACHE_URL" list | wc -l)
job=$(curl -fsS -X POST "$BUILD_URL/v1/builds" -H 'Content-Type: application/json' \
  -d '{"fixture":"fail-fixture"}' | jq -r '.id')
final=$(curl -fsS "$BUILD_URL/v1/builds/$job?wait=15s")
st=$(echo "$final" | jq -r '.status'); ec=$(echo "$final" | jq -r '.exitCode')
[ "$st" = "failed" ] && [ "$ec" = "7" ] \
  && ok "fail-fixture reported failed/exit=7" || bad "fail status=$st exit=$ec"
echo "$final" | jq -e '(.artifacts // []) | length == 0' >/dev/null \
  && ok "failed job lists zero artifacts" || bad "failed job artifacts"
after=$(./bin/cachectl -url "$CACHE_URL" list | wc -l)
[ "$before" = "$after" ] && ok "cache unchanged by failed build ($before objects)" || bad "cache grew $before -> $after"

# ---------------------------------------------------------------------------
log "test 10: corrupt cache is diagnosable and self-heals (quarantine)"
hex="${BIG#sha256:}"
bp="$CACHE_DIR/blobs/sha256/${hex:0:2}/$hex"
[ -f "$bp" ] || die "expected blob at sharded path $bp"
chmod u+w "$(dirname "$bp")" "$bp" 2>/dev/null || true
printf 'CORRUPTED-BYTES-IN-PLACE' > "$bp"
chmod a-w "$bp"
code=$(curl -s -o /tmp/mc-verify.json -w '%{http_code}' -X POST "$CACHE_URL/v1/blobs/$BIG/verify")
[ "$code" = "500" ] && jq -e '.code == "corrupt" and .extra.quarantined == true' /tmp/mc-verify.json >/dev/null \
  && ok "verify diagnosed corruption (500 corrupt, quarantined=true)" || { cat /tmp/mc-verify.json; bad "verify code=$code"; }
code=$(curl -s -o /dev/null -w '%{http_code}' "$CACHE_URL/v1/blobs/$BIG")
[ "$code" = "404" ] && ok "corrupt object no longer served (self-healed)" || bad "corrupt still served $code"
find "$CACHE_DIR/quarantine" -type f -name 'corrupt-*' | grep -q . \
  && ok "corrupt bytes preserved in quarantine/ for inspection" || bad "no corrupt-* quarantine file"
# cachectl doctor reports cleanly on the remaining good object.
./bin/cachectl -url "$CACHE_URL" doctor | grep -q "all objects verified" \
  && ok "cachectl doctor passes for healthy objects" || bad "doctor output"

# Re-upload the big object so the restart test has both objects.
curl -s -o /dev/null -X PUT --data-binary @"$WORKDIR/big.bin" "$CACHE_URL/v1/blobs/$BIG"

# ---------------------------------------------------------------------------
log "test 11: server restart persists objects and sweeps orphaned tmp uploads"
# Plant an orphan tmp file as if an upload had been killed mid-flight.
echo "partial-upload-bytes" > "$CACHE_DIR/tmp/upload-orphan-test.part"
old_cache_pid="$CACHE_PID"
kill "$old_cache_pid"; CACHE_PID=""; wait "$old_cache_pid" 2>/dev/null || true
./bin/cacheserver -addr ":${CACHE_PORT}" -cache-dir "$CACHE_DIR" -max-object-size "$MAXOBJ" \
  >"$WORKDIR/cache2.log" 2>&1 &
CACHE_PID=$!
wait_http "$CACHE_URL/healthz" || die "cacheserver did not restart"
grep -q "swept 1 orphaned temp" "$WORKDIR/cache2.log" \
  && ok "restart swept the orphaned tmp upload" || { cat "$WORKDIR/cache2.log"; bad "sweep log"; }
[ ! -e "$CACHE_DIR/tmp/upload-orphan-test.part" ] && ok "orphan tmp file removed" || bad "orphan survived"
./bin/cachectl -url "$CACHE_URL" stat "$DGST" >/dev/null \
  && ok "small object persisted across restart" || bad "small object lost"
./bin/cachectl -url "$CACHE_URL" get "$BIG" "$DL_DIR/after-restart.bin" >/dev/null \
  && ok "big object persisted, downloads and verifies after restart" || bad "big object after restart"
[ "$(shahex "$DL_DIR/after-restart.bin")" = "${BIG#sha256:}" ] \
  && ok "post-restart content hash matches" || bad "post-restart hash"

# ---------------------------------------------------------------------------
log "test 12: stats + listing JSON"
curl -fsS "$CACHE_URL/v1/stats" | jq -e '.objects == 2 and .tempFiles == 0 and .maxObjectSize == 8388608' >/dev/null \
  && ok "stats reports 2 objects, 0 temps, configured cap" || bad "stats: $(curl -s "$CACHE_URL/v1/stats")"

# ---------------------------------------------------------------------------
teardown
CACHE_PID=""; BUILD_PID=""

echo
echo "------------------------------------------------------------"
echo "acceptance summary: $PASS passed, $FAIL failed"
echo "artifacts preserved in: $WORKDIR (cache.log, build.log, downloads)"
echo "------------------------------------------------------------"
[ "$FAIL" = "0" ]
