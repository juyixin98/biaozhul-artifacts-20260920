#!/usr/bin/env bash
# acceptance.sh — hermetic, offline acceptance test for mirror-admission.
#
# It does NOT touch a cluster or registry. It:
#   1. builds all binaries with GOPROXY=off against the vendored deps,
#   2. runs the full Go test suite (crypto, policy freeze, service, HTTP),
#   3. boots the server and posts every genuinely-signed example,
#   4. asserts the exact decision for each scenario,
#   5. asserts expiry-boundary semantics (not_after == now => expired),
#   6. asserts append-only immutability (re-eval => new id, old report intact),
#   7. asserts a tampered policy is refused by the freeze lock.
#
# Exit non-zero on the first failed assertion.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

EX=examples/generated
WORK="$(mktemp -d)"
trap 'set +e; for p in $PORTS; do fuser -k ${p}/tcp >/dev/null 2>&1 || true; done; wait 2>/dev/null || true; rm -rf "$WORK"' EXIT
PORTS=""
PASS=0
FAIL=0

red()   { printf '\033[31m%s\033[0m\n' "$*"; }
grn()   { printf '\033[32m%s\033[0m\n' "$*"; }
step()  { printf '\n== %s ==\n' "$*"; }

check() { # <desc> <expected> <actual>
  if [ "$2" = "$3" ]; then
    grn "PASS: $1 ($3)"; PASS=$((PASS+1))
  else
    red "FAIL: $1 — expected [$2], got [$3]"; FAIL=$((FAIL+1))
  fi
}

# 1. Offline build against vendored dependencies --------------------------------
step "1. offline build (GOPROXY=off, -mod=vendor)"
GOPROXY=off GOFLAGS=-mod=vendor go build -o "$WORK/server" ./cmd/server
GOPROXY=off GOFLAGS=-mod=vendor go build -o "$WORK/verifier" ./cmd/verifier
grn "binaries built without network"

# Examples + lock must already be committed; regenerate keys would change ids.
[ -f "$EX/keys/tester_public.pem" ] || { red "examples missing — run: make examples"; exit 1; }
[ -f policy/policy_freeze.lock.json ] || { red "freeze lock missing — run: make policy-lock"; exit 1; }

# 2. Go test suite --------------------------------------------------------------
step "2. go test suite (real crypto, no cluster)"
GOPROXY=off GOFLAGS=-mod=vendor go test -count=1 ./... | tee "$WORK/gotest.log"
grep -q '^ok' "$WORK/gotest.log" && grn "go tests reported ok"

# 3. Boot servers (normal clock + two fixed-boundary clocks) --------------------
start() { # <name> <port> [NOW]
  local name="$1" port="$2" now="${3:-}"
  local args=(env
    MIRRORAD_VERIFIER_PUB="$EX/keys/tester_public.pem"
    MIRRORAD_EXEMPT_PUB="$EX/keys/exempt_authority_public.pem"
    MIRRORAD_ALLOWLIST="$EX/allowlist.json"
    MIRRORAD_REPORTS="$WORK/$name.jsonl")
  if [ -n "$now" ]; then
    args+=(MIRRORAD_NOW="$now")
  fi
  args+=(MIRRORAD_LISTEN=":$port" "$WORK/server")
  "${args[@]}" >"$WORK/$name.log" 2>&1 &
  disown 2>/dev/null || true
  PORTS="$PORTS $port"
  for _ in $(seq 1 40); do
    curl -sf "http://127.0.0.1:$port/healthz" >/dev/null 2>&1 && return 0
    sleep 0.25
  done
  red "server $name never became healthy"; cat "$WORK/$name.log"; exit 1
}

step "3. boot servers (normal + boundary clocks)"
start a 18080
start b 18081 "2026-10-01T00:00:00Z"
start c 18082 "2026-09-30T23:59:59Z"
grn "three instances healthy"

decision_of() { # <port> <file>
  curl -s -X POST "http://127.0.0.1:$1/v1/admission/evaluate" \
    -H 'Content-Type: application/json' --data-binary @"$2" \
    | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("decision") or d.get("error"))'
}

# 4. Scenario decisions ---------------------------------------------------------
step "4. signed scenarios"
# name:expected
SCENARIOS=(
  "01-allow-clean:ALLOW"
  "02-deny-root:DENY"
  "03-deny-uid0:DENY"
  "04-deny-privileged:DENY"
  "05-deny-sysadmin-cap:DENY"
  "06-deny-base-not-allowed:DENY"
  "07-unknown-missing-user:UNKNOWN"
  "08-unknown-no-attestation:UNKNOWN"
  "09-deny-verifier-fail:DENY"
  "10-exempt-root-valid:ALLOW"
  "12-exempt-wrong-digest:DENY"
)
for entry in "${SCENARIOS[@]}"; do
  name="${entry%%:*}"; want="${entry##*:}"
  got="$(decision_of 18080 "$EX/requests/$name.request.json")"
  check "$name" "$want" "$got"
done

# 5. Boundary time --------------------------------------------------------------
step "5. expiry boundary (not_after 2026-10-01T00:00:00Z)"
got="$(decision_of 18081 "$EX/requests/11-exempt-boundary-expired.request.json")"
check "11 at exact expiry" "DENY" "$got"
got="$(decision_of 18082 "$EX/requests/11-exempt-boundary-expired.request.json")"
check "11 one second before expiry" "ALLOW" "$got"

# 6. Append-only immutability ---------------------------------------------------
step "6. re-evaluation appends, never overwrites"
first="$(decision_of 18080 "$EX/requests/01-allow-clean.request.json")"
id1="$(curl -s -X POST http://127.0.0.1:18080/v1/admission/evaluate -H 'Content-Type: application/json' \
  --data-binary @"$EX/requests/01-allow-clean.request.json" | python3 -c 'import sys,json; print(json.load(sys.stdin)["id"])')"
id2="$(curl -s -X POST http://127.0.0.1:18080/v1/admission/evaluate -H 'Content-Type: application/json' \
  --data-binary @"$EX/requests/01-allow-clean.request.json" | python3 -c 'import sys,json; print(json.load(sys.stdin)["id"])')"
[ "$id1" != "$id2" ] && { grn "PASS: distinct report ids on re-evaluation ($id1 != $id2)"; PASS=$((PASS+1)); } || { red "FAIL: report id reused"; FAIL=$((FAIL+1)); }
old="$(curl -s "http://127.0.0.1:18080/v1/reports/$id1" | python3 -c 'import sys,json; print(json.load(sys.stdin)["decision"])')"
check "old report still intact" "ALLOW" "$old"
n_reports="$(wc -l < "$WORK/a.jsonl" | tr -d ' ')"
# 11 scenario posts + 2 extra re-evals + scenario 01 earlier counts; just assert growth >= 13
[ "$n_reports" -ge 13 ] && { grn "PASS: JSONL append store has $n_reports lines"; PASS=$((PASS+1)); } || { red "FAIL: expected >=13 persisted lines, got $n_reports"; FAIL=$((FAIL+1)); }

# 7. Tampered policy must be refused --------------------------------------------
step "7. freeze lock rejects tampered policy"
sed 's/version := "1.4.0"/version := "9.9.9"/' policy/admission.rego > "$WORK/tampered.rego"
set +e
env MIRRORAD_POLICY="$WORK/tampered.rego" MIRRORAD_POLICY_LOCK=policy/policy_freeze.lock.json \
  MIRRORAD_LISTEN=:18099 MIRRORAD_VERIFIER_PUB="$EX/keys/tester_public.pem" \
  MIRRORAD_EXEMPT_PUB="$EX/keys/exempt_authority_public.pem" \
  MIRRORAD_ALLOWLIST="$EX/allowlist.json" MIRRORAD_REPORTS="$WORK/t.jsonl" \
  "$WORK/server" >"$WORK/tampered.log" 2>&1
rc=$?
set -e
if [ "$rc" -ne 0 ] && grep -q "POLICY FREEZE VIOLATION" "$WORK/tampered.log"; then
  grn "PASS: tampered policy refused at startup"; PASS=$((PASS+1))
else
  red "FAIL: tampered policy was accepted (rc=$rc)"; FAIL=$((FAIL+1))
fi

# 8. Cross-signed exemption rejected --------------------------------------------
step "8. tester key cannot mint exemptions"
cfg="$EX/configs/02-deny-root.json"
imgd="$(python3 - "$cfg" <<'PY'
import sys,json,hashlib
b=open(sys.argv[1],'rb').read()
c=json.dumps(json.loads(b),sort_keys=True,separators=(',',':'))
print('sha256:'+hashlib.sha256(c.encode()).hexdigest())
PY
)"
"$WORK/verifier" exempt -image-digest "$imgd" -rule no_root_user \
  -not-after 2026-12-31T00:00:00Z -id forged -reason x \
  -key "$EX/keys/tester_private.pem" > "$WORK/forged-ex.json"
python3 - "$cfg" "$EX/sboms/02-deny-root.json" "$EX/attestations/02-deny-root.json" "$WORK/forged-ex.json" <<'PY' > "$WORK/forged.req.json"
import sys,json
cfg,sbom,att,ex=map(lambda p:json.load(open(p)),sys.argv[1:])
json.dump({"image_config":cfg,"sbom":sbom,"attestation":att,"exemptions":[ex],
           "allowlist_version":"allowlist-2026.09.24"},open(sys.stdout.fileno(),'w'))
PY
code="$(curl -s -o "$WORK/forged.resp" -w '%{http_code}' -X POST http://127.0.0.1:18080/v1/admission/evaluate \
  -H 'Content-Type: application/json' --data-binary @"$WORK/forged.req.json")"
check "cross-signed waiver HTTP status" "400" "$code"

# Summary -----------------------------------------------------------------------
printf '\n================ ACCEPTANCE SUMMARY ================\n'
printf 'passed: %d   failed: %d\n' "$PASS" "$FAIL"
if [ "$FAIL" -ne 0 ]; then
  red "ACCEPTANCE FAILED"
  exit 1
fi
grn "ACCEPTANCE PASSED — no cluster was contacted"
