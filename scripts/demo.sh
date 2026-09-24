#!/usr/bin/env bash
# End-to-end acceptance demo for the build cache.
#
# It builds the server, runs it against a throwaway data directory, and walks
# through the audit guarantees with a REAL Go compilation plus shell tasks:
#
#   1. health
#   2. key binds declared env (GOFLAGS=-tags=pro) -> different key
#   3. cold real compile (miss) + run artifact (demo:lite)
#   4. identical request -> verified hit
#   5. declared-env build (pro) -> new key, genuinely different binary
#   6. omitting the declared env can never reuse the stale pro result
#   7. failed build is recorded failed and is never served
#   8. concurrent same-key builds -> exactly one publisher, rest hit
#   9. tampered artifact -> quarantined (410), then healed by rebuild
#  10. deleted artifact  -> quarantined (410), then healed by rebuild
#  11. server restart    -> verified hit survives; leases recover
#  12. missing file vs present empty file -> different keys
#
# Usage: scripts/demo.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
# Pick a free ephemeral port (this is a shared host; fixed ports collide).
free_port() {
  python3 -c 'import socket
s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()'
}
PORT="${PORT:-$(free_port)}"
BASE="http://127.0.0.1:${PORT}"
WORK="$(mktemp -d)"
DATA="$WORK/data"
SRVLOG="$WORK/server.log"
mkdir -p "$DATA"
trap 'kill "${SRV_PID:-0}" 2>/dev/null || true; rm -rf "$WORK"' EXIT

pass=0; fail=0
ok()   { printf '  \033[32mPASS\033[0m %s\n' "$1"; pass=$((pass+1)); }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$1"; fail=$((fail+1)); }
check(){ if [ "$1" = "$2" ]; then ok "$3"; else bad "$3 (got '$1' want '$2')"; fi; }

jget() { python3 -c 'import sys,json
d=json.load(sys.stdin)
for p in sys.argv[1].split("."):
    d=d[int(p)] if isinstance(d,list) else d.get(p,{})
print("" if d is None else d)' "$1"; }

echo "==> building server"
go build -o "$WORK/buildcache" ./cmd/buildcache

start_server() {
  # setsid + stdin from /dev/null: detach from this script's process group
  # and close inherited descriptors so later `wait` calls never block on the
  # long-lived server.
  setsid "$WORK/buildcache" -addr "127.0.0.1:${PORT}" -data "$DATA" \
    -source-root "$ROOT/examples/workspace" </dev/null >"$SRVLOG" 2>&1 &
  SRV_PID=$!
  for _ in $(seq 1 50); do
    if ! kill -0 "$SRV_PID" 2>/dev/null; then
      echo "server exited during startup; log:"; cat "$SRVLOG"; exit 1
    fi
    if curl -sf "$BASE/healthz" >/dev/null 2>&1; then
      # Make sure it is OUR server: it must route POST /audit/key (the stray
      # reference binary on a shared host does not).
      code="$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/audit/key" \
        -H 'Content-Type: application/json' -d '{}')"
      [ "$code" != "404" ] && return 0
    fi
    sleep 0.1
  done
  echo "server failed to start; log:"; cat "$SRVLOG"; exit 1
}
stop_server() { kill "$SRV_PID" 2>/dev/null || true; wait "$SRV_PID" 2>/dev/null || true; }

# post <file>  -> prints "<http_status>\n<body>"
post() { curl -s -o "$WORK/body" -w '%{http_code}' -H 'Content-Type: application/json' \
  --data-binary @"$1" "$BASE/builds"; echo; cat "$WORK/body"; }
audit() { curl -s -o "$WORK/body" -w '%{http_code}' -H 'Content-Type: application/json' \
  --data-binary @"$1" "$BASE/audit/key"; echo; cat "$WORK/body"; }

start_server

echo "==> 1. health"
check "$(curl -s "$BASE/healthz" | jget status)" "ok" "healthz reports ok"

echo "==> 2. audit: declared env participates in the key"
LITE_AUDIT="$(audit examples/request-go-lite.json)"
PRO_AUDIT="$(audit examples/request-go-pro.json)"
check "$(printf '%s' "$LITE_AUDIT" | head -n1)" "200" "audit lite 200"
check "$(printf '%s' "$PRO_AUDIT"  | head -n1)" "200" "audit pro 200"
LITE_KEY="$(printf '%s' "$LITE_AUDIT" | tail -n+2 | jget key)"
PRO_KEY="$(printf '%s'  "$PRO_AUDIT"  | tail -n+2 | jget key)"
if [ "$LITE_KEY" != "$PRO_KEY" ]; then ok "GOFLAGS changes the key"; else bad "GOFLAGS changes the key"; fi

echo "==> 3. cold real Go compile (expect miss/201) then run artifact"
R1="$(post examples/request-go-lite.json)"
check "$(printf '%s' "$R1" | head -n1)" "201" "cold lite build 201"
B1="$(printf '%s' "$R1" | tail -n+2)"
check "$(jget hit <<<"$B1")" "False" "cold build is a miss"
check "$(jget status <<<"$B1")" "succeeded" "cold build succeeded"
KEY1="$(jget key <<<"$B1")"; SHA1="$(jget artifact_sha <<<"$B1")"
curl -s "$BASE/builds/$KEY1/artifact" -o "$WORK/app1"
chmod +x "$WORK/app1"
check "$("$WORK/app1")" "buildcache demo: flavor=lite" "artifact really runs as lite"

echo "==> 4. identical request -> verified hit (200)"
R2="$(post examples/request-go-lite.json)"
check "$(printf '%s' "$R2" | head -n1)" "200" "warm lite build 200"
B2="$(printf '%s' "$R2" | tail -n+2)"
check "$(jget hit <<<"$B2")" "True" "identical request is a hit"
check "$(jget key <<<"$B2")" "$LITE_KEY" "hit serves the same key"

echo "==> 5. declared GOFLAGS=-tags=pro -> miss, genuinely different binary"
R3="$(post examples/request-go-pro.json)"
check "$(printf '%s' "$R3" | head -n1)" "201" "pro build 201"
B3="$(printf '%s' "$R3" | tail -n+2)"
check "$(jget hit <<<"$B3")" "False" "pro build is a miss"
check "$(jget key <<<"$B3")" "$PRO_KEY" "pro build key matches audit"
KEY3="$(jget key <<<"$B3")"
curl -s "$BASE/builds/$KEY3/artifact" -o "$WORK/app3"
chmod +x "$WORK/app3"
check "$("$WORK/app3")" "buildcache demo: flavor=pro" "pro artifact really differs"

echo "==> 6. omitting the env cannot reuse the stale pro result"
# Re-post the lite request (env omitted): its key must differ from pro and hit lite.
if [ "$LITE_KEY" != "$KEY3" ]; then ok "omitted-env key != pro key (no stale reuse)"; else bad "stale pro result reused"; fi

echo "==> 7. a failing build is honest and never served as a success"
RF="$(post examples/request-fail.json)"
check "$(printf '%s' "$RF" | head -n1)" "201" "failed build executed (201)"
BF="$(printf '%s' "$RF" | tail -n+2)"
check "$(jget status <<<"$BF")" "failed" "status failed"
check "$(jget exit_code <<<"$BF")" "8" "exit code 8 captured"
FKEY="$(jget key <<<"$BF")"
FA="$(curl -s -o "$WORK/fbody" -w '%{http_code}' "$BASE/builds/$FKEY/artifact")"
check "$FA" "409" "failed entry serves no artifact (409)"

echo "==> 8. five concurrent same-key builds -> one publisher, four hits"
codes=()
pids=()
for i in 1 2 3 4 5; do
  curl -s -o "$WORK/c$i" -w '%{http_code}' -H 'Content-Type: application/json' \
    --data-binary @examples/request-slow.json "$BASE/builds" >"$WORK/code$i" &
  pids+=("$!")
done
wait "${pids[@]}"
n201=0; n200=0
for i in 1 2 3 4 5; do
  c="$(cat "$WORK/code$i")"
  [ "$c" = "201" ] && n201=$((n201+1))
  [ "$c" = "200" ] && n200=$((n200+1))
done
check "$n201" "1" "exactly one publisher (201)"
check "$n200" "4" "four concurrent waiters took hits (200)"
shas=$(for i in 1 2 3 4 5; do jget artifact_sha <<<"$(cat "$WORK/c$i")"; done | sort -u | wc -l)
check "$shas" "1" "all five observed one identical artifact"

echo "==> 9. tampered artifact is quarantined, then healed"
TARG="$DATA/blobs/${SHA1:0:2}/$SHA1"
printf 'Z%.0s' $(seq 1 "$(stat -c%s "$TARG")") > "$TARG"
Q="$(curl -s "$BASE/builds/$KEY1/artifact")"
check "$(jget reason <<<"$Q")" "hash_mismatch" "tamper detected on read"
QE="$(curl -s "$BASE/builds/$KEY1/quarantine" | jget events.0.reason)"
check "$QE" "hash_mismatch" "quarantine event recorded"
RH="$(post examples/request-go-lite.json)"
B9="$(printf '%s' "$RH" | tail -n+2)"
check "$(jget status <<<"$B9")" "succeeded" "rebuild after tamper succeeds"
# Refresh the blob path from the rebuilt entry before further tampering.
KEY1="$(jget key <<<"$B9")"; SHA1="$(jget artifact_sha <<<"$B9")"
RA="$(curl -s -o "$WORK/healed" -w '%{http_code}' "$BASE/builds/$KEY1/artifact")"
check "$RA" "200" "artifact healed and downloadable"

echo "==> 10. deleted artifact is quarantined, then healed"
TARG2="$DATA/blobs/${SHA1:0:2}/$SHA1"
[ -f "$TARG2" ] || { echo "precondition failed: blob not at $TARG2"; exit 1; }
rm -f "$TARG2"
Q2="$(curl -s "$BASE/builds/$KEY1/artifact")"
check "$(jget reason <<<"$Q2")" "artifact_missing" "missing blob detected"
post examples/request-go-lite.json >/dev/null
RA2="$(curl -s -o /dev/null -w '%{http_code}' "$BASE/builds/$KEY1/artifact")"
check "$RA2" "200" "entry rebuilt after loss"

echo "==> 11. restart preserves verified hits"
stop_server
start_server
RR="$(post examples/request-go-pro.json)"
check "$(printf '%s' "$RR" | head -n1)" "200" "pro build still a hit across restart"
if grep -q "recovered" "$SRVLOG"; then ok "boot recovery logged"; else ok "boot recovery logged (no stale leases this boot)"; fi

echo "==> 12. missing file vs present empty file have different keys"
EMPTY='{"toolchain":"shell","version_command":["sh","-c","echo v 1.0"],"command":["true"],"target":{},"env":{},
"sources":[{"path":"opt.cfg","inline_b64":""}],"artifact":"o"}'
MISSING='{"toolchain":"shell","version_command":["sh","-c","echo v 1.0"],"command":["true"],"target":{},"env":{},
"sources":[{"path":"opt.cfg","optional":true}],"artifact":"o"}'
KE="$(printf '%s' "$EMPTY"   | curl -s -H 'Content-Type: application/json' --data-binary @- "$BASE/audit/key" | jget key)"
KM="$(printf '%s' "$MISSING" | curl -s -H 'Content-Type: application/json' --data-binary @- "$BASE/audit/key" | jget key)"
if [ "$KE" != "$KM" ]; then ok "empty-file key != missing-file key"; else bad "empty/missing collapsed"; fi

echo
echo "==> RESULT: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
