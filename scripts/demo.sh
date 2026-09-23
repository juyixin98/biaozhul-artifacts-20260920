#!/usr/bin/env bash
# End-to-end acceptance demo for the TWAP service.
#
# It builds the binaries, starts the real server against real PostgreSQL,
# registers two HMAC-authenticated sources, ingests unequal-interval samples,
# and exercises: time-weighted (not mean) value, window boundaries, source
# conflicts, in-horizon late correction with new versions, out-of-horizon
# rejection, replay/bad-signature rejection, and incremental-vs-full rebuild
# equivalence.
#
# Requirements: Go 1.22+, running PostgreSQL (run scripts/setup-db.sh first),
# curl, jq.
set -euo pipefail

cd "$(dirname "$0")/.."

ADDR="127.0.0.1:18123"
BASE_URL="http://${ADDR}"
DSN="${TWAP_DSN:-postgres://twap:twappw@127.0.0.1:5432/twap?sslmode=disable}"
ADMIN_KEY="${TWAP_ADMIN_KEY:-demo-admin-key}"
# Demo-only secrets (32 raw bytes, base64). Never reuse in production.
SECRET_A="ZzWGr3bni0HAAAPdJjj7KSHgBFIG1nEwfPQMK9NVMAY="
SECRET_B="03qMTthbnXfBvymrikhztotPrOFhJpxR4dqXaJ9mfvQ="

PASS=0; FAIL=0
ok()   { printf '  \033[32m[ OK ]\033[0m %s\n' "$1"; PASS=$((PASS+1)); }
bad()  { printf '  \033[31m[FAIL]\033[0m %s\n' "$1"; FAIL=$((FAIL+1)); }
info() { printf '\n\033[1;36m== %s ==\033[0m\n' "$1"; }
die()  { printf 'FATAL: %s\n' "$1" >&2; exit 1; }

assert_eq() { # expected actual message
  if [[ "$1" == "$2" ]]; then ok "$3 (= $2)"; else bad "$3: want '$1' got '$2'"; fi
}

# ---- build ----
info "Building binaries"
mkdir -p bin
go build -o bin/twap-server ./cmd/server
go build -o bin/twap-sign   ./cmd/sign
ok "binaries built"

# ---- start server ----
info "Starting server on ${ADDR}"
TWAP_DSN="${DSN}" TWAP_ADDR="${ADDR}" TWAP_ADMIN_KEY="${ADMIN_KEY}" \
  ./bin/twap-server >/tmp/twap-demo.log 2>&1 &
SRV_PID=$!
trap 'kill ${SRV_PID} 2>/dev/null || true' EXIT
for _ in $(seq 1 50); do
  curl -sf "${BASE_URL}/healthz" >/dev/null && break
  sleep 0.1
done
curl -sf "${BASE_URL}/readyz" >/dev/null || { cat /tmp/twap-demo.log; die "server not ready"; }
ok "server healthy"

# Make the demo repeatable: clear data left from previous runs.
psql "$(sed 's|postgres://|postgresql://|' <<<"${DSN}")" -q -c \
  "TRUNCATE window_versions, used_nonces, samples, sources RESTART IDENTITY CASCADE;" \
  && ok "demo tables reset" || true

# ---- admin auth ----
info "Registering authenticated sources (HMAC)"
admin_post() { # path json
  curl -s -o /dev/null -w '%{http_code}' -u "admin:${ADMIN_KEY}" \
    -H 'Content-Type: application/json' -X POST "${BASE_URL}$1" -d "$2"
}
[[ "$(admin_post /v1/admin/sources "{\"name\":\"venueA\",\"priority\":10,\"secret\":\"${SECRET_A}\"}")" == "201" ]] \
  && ok "venueA registered (priority 10)" || bad "register venueA"
[[ "$(admin_post /v1/admin/sources "{\"name\":\"venueB\",\"priority\":0,\"secret\":\"${SECRET_B}\"}")" == "201" ]] \
  && ok "venueB registered (priority 0)" || bad "register venueB"

# Admin endpoint requires auth.
[[ "$(curl -s -o /dev/null -w '%{http_code}' -X POST "${BASE_URL}/v1/admin/rebuild")" == "401" ]] \
  && ok "admin endpoint rejects unauthenticated call" || bad "admin auth"

# ---- signed ingest helper ----
# ingest SOURCE SECRET TS_US PRICE SYMBOL -> http code
ingest_code() {
  local source="$1" secret="$2" ts_us="$3" price="$4" symbol="$5" nonce
  nonce="$(openssl rand -hex 16)"
  local body sig
  body=$(jq -nc --arg s "$symbol" --argjson t "$ts_us" --argjson p "$price" \
    '{symbol:$s, ts_us:$t, price:$p}')
  sig=$(printf '%s' "$body" | ./bin/twap-sign -secret "$secret" -ts-us "$ts_us" -nonce "$nonce")
  curl -s -o /dev/null -w '%{http_code}' \
    -H 'Content-Type: application/json' \
    -H "X-Source: ${source}" -H "X-Timestamp-Usec: ${ts_us}" \
    -H "X-Nonce: ${nonce}" -H "X-Signature: ${sig}" \
    -X POST "${BASE_URL}/v1/samples" -d "$body"
}
# Same, but returns "code body" and lets caller pin the nonce (replay test).
ingest_full() {
  local source="$1" secret="$2" ts_us="$3" price="$4" symbol="$5" nonce="$6"
  local body sig
  body=$(jq -nc --arg s "$symbol" --argjson t "$ts_us" --argjson p "$price" \
    '{symbol:$s, ts_us:$t, price:$p}')
  sig=$(printf '%s' "$body" | ./bin/twap-sign -secret "$secret" -ts-us "$ts_us" -nonce "$nonce")
  local out
  out=$(curl -s -w '\n%{http_code}' \
    -H 'Content-Type: application/json' \
    -H "X-Source: ${source}" -H "X-Timestamp-Usec: ${ts_us}" \
    -H "X-Nonce: ${nonce}" -H "X-Signature: ${sig}" \
    -X POST "${BASE_URL}/v1/samples" -d "$body")
  printf '%s' "$out"
}

# Work on a window that completed ~3 minutes ago: samples are recent enough to
# be accepted (5-minute late window) but the window is fully observable now.
NOW_S=$(date +%s)
BASE_S=$(( (NOW_S - 180) / 60 * 60 ))
us() { printf '%s000000' "$1"; }
TS=$(us $BASE_S)
REQ_NOW=$(us $NOW_S)

# ---- 1. unequal intervals: TWAP must not equal the arithmetic mean ----
info "Unequal-interval window BTC/USD @ ${BASE_S}"
# p=10000 for [0,20), p=10060 for [20,50), p=10120 for [50,60)
[[ "$(ingest_code venueA "$SECRET_A" "$(us $((BASE_S+0)))"  10000 BTC/USD)" == "201" ]] \
  && ok "sample t+0  10000 accepted" || bad "sample t+0"
[[ "$(ingest_code venueB "$SECRET_B" "$(us $((BASE_S+20)))" 10060 BTC/USD)" == "201" ]] \
  && ok "sample t+20 10060 accepted" || bad "sample t+20"
[[ "$(ingest_code venueA "$SECRET_A" "$(us $((BASE_S+50)))" 10120 BTC/USD)" == "201" ]] \
  && ok "sample t+50 10120 accepted" || bad "sample t+50"

W=$(curl -s "${BASE_URL}/v1/symbols/BTC%2FUSD/windows/${BASE_S}")
echo "    window: $(echo "$W" | jq -c '{twap,coverage,stale,version}')"
assert_eq "10050.000000" "$(echo "$W" | jq -r '.twap')" \
  "TWAP is time-weighted (mean of samples would wrongly be 10060.000000)"
assert_eq "1.000000" "$(echo "$W" | jq -r '.coverage')" "full coverage"
assert_eq "false" "$(echo "$W" | jq -r '.stale')" "completed window not stale"
HASH_V1=$(echo "$W" | jq -r .content_hash)

# ---- 2. boundary ownership ----
info "Window boundary sample belongs to the next half-open window"
[[ "$(ingest_code venueA "$SECRET_A" "$(us $((BASE_S+60)))" 10999 BTC/USD)" == "201" ]] \
  && ok "boundary sample t+60 accepted" || bad "boundary sample"
W2=$(curl -s "${BASE_URL}/v1/symbols/BTC%2FUSD/windows/${BASE_S}")
assert_eq "10050.000000" "$(echo "$W2" | jq -r '.twap')" "first window unaffected by t=end sample"
W3=$(curl -s "${BASE_URL}/v1/symbols/ETH%2FUSD/windows/${BASE_S}")
# ETH not yet ingested — expect empty.
assert_eq "" "$(echo "$W3" | jq -r '.twap')" "unknown symbol reads as empty window"
assert_eq "true" "$(echo "$W3" | jq -r '.stale')" "empty window is stale"

# ---- 3. source conflict at a duplicate timestamp ----
info "Duplicate timestamp / source conflict on ETH/USD"
[[ "$(ingest_code venueB "$SECRET_B" "$(us $((BASE_S+10)))" 3000 ETH/USD)" == "201" ]] \
  && ok "venueB reports 3000" || bad "eth venueB"
[[ "$(ingest_code venueA "$SECRET_A" "$(us $((BASE_S+10)))" 3100 ETH/USD)" == "201" ]] \
  && ok "venueA reports 3100 at the same ts (priority mode resolves)" || bad "eth venueA"
WE=$(curl -s "${BASE_URL}/v1/symbols/ETH%2FUSD/windows/${BASE_S}")
echo "    window: $(echo "$WE" | jq -c '{twap,conflicts,sources}')"
assert_eq "3100.000000" "$(echo "$WE" | jq -r '.twap')" "higher-priority venueA wins the timestamp"
N_CF=$(echo "$WE" | jq '.conflicts | length')
[[ "$N_CF" -ge 1 ]] && ok "losing source recorded in conflicts (n=$N_CF)" || bad "conflict not surfaced"

# ---- 4. late correction within 5 minutes => new version ----
info "Late correction (within 5 minutes) recomputes and versions"
# Insert venueA price 10080 at t+30: [30,50) changes from 10060 to 10080.
# New TWAP = 603400/60 = 10056.666667.
LATE_TS=$(us $((BASE_S+30)))
AGE=$(( NOW_S - (BASE_S+30) ))
echo "    correction is ${AGE}s old (limit 300s)"
[[ "$AGE" -lt 300 ]] || die "test timing broken"
CODE=$(ingest_code venueA "$SECRET_A" "$LATE_TS" 10080 BTC/USD)
[[ "$CODE" == "200" || "$CODE" == "201" ]] && ok "late correction accepted ($CODE)" || bad "late correction $CODE"
W4=$(curl -s "${BASE_URL}/v1/symbols/BTC%2FUSD/windows/${BASE_S}")
echo "    window: $(echo "$W4" | jq -c '{twap,version,coverage}')"
assert_eq "10056.666667" "$(echo "$W4" | jq -r '.twap')" "recomputed TWAP after late correction"
V2=$(echo "$W4" | jq -r '.version')
[[ "$V2" -ge 2 ]] && ok "window version advanced to v${V2}" || bad "version did not advance (v=$V2)"
[[ "$(echo "$W4" | jq -r .content_hash)" != "$HASH_V1" ]] && ok "content hash changed" || bad "hash unchanged"

# ---- 5. late data beyond 5 minutes is refused ----
info "Samples older than 5 minutes are refused (no future-fill of history)"
TOO_OLD=$(us $((NOW_S-360)))
# Request is signed NOW (passes the transport skew check); the SAMPLE is 6
# minutes old, which the service must reject at the business layer with 422.
OLD_BODY=$(jq -nc --argjson t "$TOO_OLD" '{symbol:"BTC/USD", ts_us:$t, price:1}')
OLD_NONCE="old-$(openssl rand -hex 8)"
OLD_SIG=$(printf '%s' "$OLD_BODY" | ./bin/twap-sign -secret "$SECRET_A" -ts-us "$REQ_NOW" -nonce "$OLD_NONCE")
CODE=$(curl -s -o /dev/null -w '%{http_code}' \
  -H 'Content-Type: application/json' -H "X-Source: venueA" \
  -H "X-Timestamp-Usec: ${REQ_NOW}" -H "X-Nonce: ${OLD_NONCE}" \
  -H "X-Signature: ${OLD_SIG}" \
  -X POST "${BASE_URL}/v1/samples" -d "$OLD_BODY")
assert_eq "422" "$CODE" "6-minute-old sample rejected with 422"

# ---- 6. replay protection and signature integrity ----
info "Replay protection (nonce) and HMAC integrity"
NONCE="fixed-nonce-$(openssl rand -hex 8)"
OUT=$(ingest_full venueA "$SECRET_A" "$REQ_NOW" 7777 "XRP/USD" "$NONCE")
C1=$(printf '%s' "$OUT" | tail -1)
assert_eq "201" "$C1" "first signed request accepted"
OUT=$(ingest_full venueA "$SECRET_A" "$REQ_NOW" 7777 "XRP/USD" "$NONCE")
C2=$(printf '%s' "$OUT" | tail -1)
assert_eq "409" "$C2" "replayed identical request (same nonce) rejected"
# Tamper price but keep the same valid-looking header set: recompute MAC? No —
# send a body that does not match the signature by reusing prior signature.
BODY='{"symbol":"XRP/USD","ts_us":'"$REQ_NOW"',"price":7778}'
BAD=$(curl -s -o /dev/null -w '%{http_code}' \
  -H 'Content-Type: application/json' -H "X-Source: venueA" \
  -H "X-Timestamp-Usec: ${REQ_NOW}" -H "X-Nonce: other-nonce-1" \
  -H "X-Signature: ${SECRET_B}" \
  -X POST "${BASE_URL}/v1/samples" -d "$BODY")
assert_eq "401" "$BAD" "invalid HMAC rejected"

# ---- 7. full rebuild must match incremental results ----
info "Incremental vs full rebuild consistency"
HASH_INC=$(curl -s "${BASE_URL}/v1/symbols/BTC%2FUSD/windows/${BASE_S}" | jq -r .content_hash)
TWAP_INC=$(curl -s "${BASE_URL}/v1/symbols/BTC%2FUSD/windows/${BASE_S}" | jq -r .twap)
RB=$(curl -s -u "admin:${ADMIN_KEY}" -X POST "${BASE_URL}/v1/admin/rebuild")
echo "    rebuild: $(echo "$RB" | jq -c .)"
HASH_FULL=$(curl -s "${BASE_URL}/v1/symbols/BTC%2FUSD/windows/${BASE_S}" | jq -r .content_hash)
TWAP_FULL=$(curl -s "${BASE_URL}/v1/symbols/BTC%2FUSD/windows/${BASE_S}" | jq -r .twap)
assert_eq "$TWAP_INC" "$TWAP_FULL" "TWAP identical after full rebuild"
assert_eq "$HASH_INC" "$HASH_FULL" "canonical hash identical after full rebuild"

# ---- 8. window listing ----
info "Listing windows"
LIST=$(curl -s "${BASE_URL}/v1/symbols/BTC%2FUSD/windows?from=${BASE_S}&to=$((BASE_S+120))")
N=$(echo "$LIST" | jq '.windows | length')
[[ "$N" -ge 1 ]] && ok "listed $N materialized window(s)" || bad "window listing empty"

# ---- summary ----
printf '\n\033[1mResult: %d passed, %d failed\033[0m\n' "$PASS" "$FAIL"
[[ "$FAIL" -eq 0 ]] || exit 1
