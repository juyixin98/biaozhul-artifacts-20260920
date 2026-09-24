#!/usr/bin/env bash
# =============================================================================
# acceptance.sh — one-shot acceptance suite for the layer-reference GC registry.
#
# It needs a running server ($BASE, default http://127.0.0.1:18080) started
# with -faults so the crash/race hooks are available. Every check performs the
# real protocol (real uploads, real SHA-256, real Postgres transactions) and
# asserts the safety properties. Exit code 0 = all passed.
# =============================================================================
set -uo pipefail
BASE="${BASE:-http://127.0.0.1:18081}"
ADDR="${ADDR:-${BASE#*://}}"
DBURL="${DBURL:-postgres://registry:registry_pw@127.0.0.1:5432/registry?sslmode=disable}"
GEN=examples/generated
mkdir -p "$GEN"
PASS=0; FAIL=0
ok()   { echo "  PASS: $1"; PASS=$((PASS+1)); }
bad()  { echo "  FAIL: $1"; FAIL=$((FAIL+1)); }
want() { # want actual message
  if [ "$1" = "$2" ]; then ok "$3"; else bad "$3 (want [$1] got [$2])"; fi; }
jget() { python3 -c "import sys,json;d=json.load(sys.stdin);$1"; }
# jgetstr <json-string> <python-expr>
jgetstr() { python3 -c "import json,sys;d=json.loads(sys.argv[1]);$2" "$1"; }

require() { command -v "$1" >/dev/null || { echo "missing tool: $1"; exit 2; }; }
require curl; require sha256sum; require python3; require psql

psqlq() { PGPASSWORD=registry_pw psql --no-psqlrc -tAc "$2" "${DBURL/\/registry?/\/${1}?}" 2>/dev/null; }

echo "Waiting for $BASE ..."
for _ in $(seq 1 30); do
  curl -fsS "$BASE/healthz" >/dev/null 2>&1 && break
  sleep 0.3
done
[ "$(curl -fsS "$BASE/healthz")" = "ok" ] || { echo "server not reachable"; exit 2; }

# Clean slate.
PGPASSWORD=registry_pw psql --no-psqlrc "$DBURL" -c "TRUNCATE gc_audit_events, gc_items, gc_runs, leases, uploads, tags, manifest_refs, manifests, blobs CASCADE;" >/dev/null

putblob() { # repo file -> digest
  local repo=$1 f=$2
  local dg="sha256:$(sha256sum "$f" | cut -d' ' -f1)"
  local code; code=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
    "$BASE/v2/$repo/blobs/uploads/?digest=$dg" --data-binary @"$f")
  [ "$code" = 201 ] || { echo "upload $f failed: $code"; exit 2; }
  echo "$dg"
}
mkman() { # config layer...  -> writes $GEN/.last-manifest and prints digest
  local cfg=$1; shift; local ls="" l
  for l in "$@"; do ls="$ls{\"mediaType\":\"application/vnd.oci.image.layer.v1.tar\",\"digest\":\"$l\",\"size\":1},"; done
  printf '{"schemaVersion":2,"mediaType":"application/vnd.oci.image.manifest.v1+json","config":{"mediaType":"application/vnd.oci.image.config.v1+json","digest":"%s","size":1},"layers":[%s]}' \
    "$cfg" "${ls%,}" > "$GEN/.last-manifest"
  echo "sha256:$(sha256sum "$GEN/.last-manifest" | cut -d' ' -f1)"
}
putman() { # repo ref file
  local out; out=$(curl -s -w $'\n%{http_code}' -X PUT -H 'Content-Type: application/vnd.oci.image.manifest.v1+json' \
    --data-binary @"$3" "$BASE/v2/$1/manifests/$2")
  local code; code=$(echo "$out" | tail -1)
  if [ "$code" != "201" ]; then echo "PUT $1/$2 -> $code body: $(echo "$out" | head -1)" >&2; fi
  echo "$code"
}

echo
echo "###################################################################"
echo "# 1. TWO IMAGES SHARE ONE LAYER"
echo "###################################################################"
echo '{"image":"alpha","os":"linux"}' > "$GEN/cfgA.json"
echo '{"image":"beta","os":"linux"}'  > "$GEN/cfgB.json"
printf 'shared-base-layer-v1\n'        > "$GEN/layer-shared.bin"
head -c 16384 /dev/urandom            > "$GEN/layer-a.bin"
head -c 32768 /dev/urandom            > "$GEN/layer-b.bin"
CA=$(putblob alpha "$GEN/cfgA.json")
CB=$(putblob beta  "$GEN/cfgB.json")
LD=$(putblob alpha "$GEN/layer-shared.bin")
AD=$(putblob alpha "$GEN/layer-a.bin")
BD=$(putblob beta  "$GEN/layer-b.bin")
mkman "$CA" "$LD" "$AD" >/dev/null; cp "$GEN/.last-manifest" "$GEN/manifest-alpha.json"; MA=$(mkman "$CA" "$LD" "$AD")
mkman "$CB" "$LD" "$BD" >/dev/null; cp "$GEN/.last-manifest" "$GEN/manifest-beta.json";  MB=$(mkman "$CB" "$LD" "$BD")
want 201 "$(putman alpha v1 "$GEN/manifest-alpha.json")" "push alpha:v1"
want 201 "$(putman beta  v1 "$GEN/manifest-beta.json")" "push beta:v1"

echo "-- GC with everything live must delete nothing"
curl -s -X POST "$BASE/admin/gc" -d '{}' > "$GEN/gc1.json"
want 0 "$(jget 'print(d["deleted_blobs"])' < "$GEN/gc1.json")" "no blob deleted while both tags live"
want 5 "$(jget 'print(d["retained_blobs"])' < "$GEN/gc1.json")" "all five blobs retained"
REASON=$(jget "print(next(i['reason'] for i in d['items'] if i['digest']=='$LD'))" < "$GEN/gc1.json")
echo "$REASON" | grep -q "alpha" && echo "$REASON" | grep -q "beta" \
  && ok "shared layer retain reason cites BOTH images" || bad "shared layer reason: $REASON"

echo
echo "###################################################################"
echo "# 2. PULL vs DELETE RACE"
echo "###################################################################"
head -c 65536 /dev/urandom > "$GEN/race.bin"
RD=$(putblob alpha "$GEN/race.bin")
# (race blob is intentionally unreferenced so GC would collect it absent a pull)
# Start a slow pull (holds advisory lock + read lease for 2.5s), GC meanwhile.
curl -s -o "$GEN/race.got" -H 'X-Read-Delay: 2500' "$BASE/v2/alpha/blobs/$RD" &
PULL=$!
sleep 0.8
curl -s -X POST "$BASE/admin/gc" -d '{}' > "$GEN/gc-race.json"
DEC=$(jget "print(next((i['decision'] for i in d['items'] if i['digest']=='$RD'),'absent'))" < "$GEN/gc-race.json")
want retain "$DEC" "blob under active pull is retained"
RSN=$(jget "print(next((i['reason'] for i in d['items'] if i['digest']=='$RD'),''))" < "$GEN/gc-race.json")
echo "$RSN" | grep -q "lease" && ok "retain reason is the active read lease" || bad "reason: $RSN"
wait $PULL
cmp -s "$GEN/race.bin" "$GEN/race.got" && ok "pulled bytes are intact despite GC" || bad "pulled bytes mismatch"
# Once pull ends, GC collects the unreferenced blob.
curl -s -X POST "$BASE/admin/gc" -d '{}' > "$GEN/gc-race2.json"
DEC2=$(jget "print(next((i['decision'] for i in d['items'] if i['digest']=='$RD'),'absent'))" < "$GEN/gc-race2.json")
want delete "$DEC2" "blob collected after pull lease released"

echo
echo "###################################################################"
echo "# 3. NEW TAG AFTER THE MARK SNAPSHOT"
echo "###################################################################"
printf 'late-tagged-layer\n' > "$GEN/late.bin"
printf '{"late":true}'      > "$GEN/late-cfg.json"
LC=$(putblob alpha "$GEN/late-cfg.json")
QL=$(putblob alpha "$GEN/late.bin")
printf '{"schemaVersion":2,"mediaType":"application/vnd.oci.image.manifest.v1+json","config":{"mediaType":"application/vnd.oci.image.config.v1+json","digest":"%s","size":1},"layers":[{"mediaType":"application/vnd.oci.image.layer.v1.tar","digest":"%s","size":1}]}' \
  "$LC" "$QL" > "$GEN/manifest-late.json"
(curl -s -X POST "$BASE/admin/gc" -d '{"mark_sweep_delay":2500}' > "$GEN/gc-late.json") &
sleep 0.9
want 201 "$(putman alpha late "$GEN/manifest-late.json")" "late tag published in the mark/sweep gap"
wait
DEC3=$(jget "print(next((i['decision'] for i in d['items'] if i['digest']=='$QL'),'absent'))" < "$GEN/gc-late.json")
want retain "$DEC3" "layer referenced during the scan is NOT deleted"
jget "print(next(i['reason'] for i in d['items'] if i['digest']=='$QL'))" < "$GEN/gc-late.json" | grep -q retained-after-mark \
  && ok "retain decided at sweep recheck (after-mark), not just the snapshot" || bad "missing retained-after-mark reason"
want 200 "$(curl -s -o /dev/null -w '%{http_code}' "$BASE/v2/alpha/manifests/late")" "late tag still resolves after GC"

echo
echo "###################################################################"
echo "# 4. GC CRASH RECOVERY (hard process kill, both windows)"
echo "###################################################################"
# 4a. kill AFTER the delete commits, BEFORE purge -> row gone, file quarantined.
head -c 4096 /dev/urandom > "$GEN/crash-after.bin"; KA=$(putblob alpha "$GEN/crash-after.bin")
curl -s -m 5 -X POST "$BASE/admin/gc?fault=kill_after_commit" -d '{}' >/dev/null 2>&1 || true
for _ in $(seq 1 20); do curl -fsS "$BASE/healthz" >/dev/null 2>&1 || break; sleep 0.3; done
sleep 0.5
want 0 "$(PGPASSWORD=registry_pw psql --no-psqlrc "$DBURL" -tAc "SELECT count(*) FROM blobs WHERE digest='$KA';")" \
  "after-commit kill: DB row is gone"
[ -n "$(find data/quarantine -name "$KA" 2>/dev/null)" ] && ok "after-commit kill: bytes held in quarantine" \
  || bad "after-commit kill: no quarantined file found"

# restart with recovery
echo "-- restarting server with -recover ..."
pkill -f 'registry -addr' 2>/dev/null; sleep 0.5
( cd "$(dirname "$0")/.." && DATABASE_URL="$DBURL" ./bin/registry -addr "$ADDR" -data ./data -faults -blob-grace 0s -recover \
   > "$GEN/server-recover.log" 2>&1 ) &
for _ in $(seq 1 30); do curl -fsS "$BASE/healthz" >/dev/null 2>&1 && break; sleep 0.3; done
want ok "$(curl -fsS "$BASE/healthz")" "server back after recovery restart"
[ -z "$(find data/quarantine -type f 2>/dev/null)" ] && ok "quarantine emptied by recovery" || bad "quarantine not empty"
grep -q "row deleted durably, quarantined file purged" "$GEN/server-recover.log" \
  && ok "after-commit recovery purged orphaned bytes" || bad "after-commit purge not logged"

# 4b. kill BEFORE commit -> tx rolls back, file quarantined -> recovery restores.
head -c 4096 /dev/urandom > "$GEN/crash-before.bin"; KB=$(putblob alpha "$GEN/crash-before.bin")
curl -s -m 5 -X POST "$BASE/admin/gc?fault=kill_before_commit" -d '{}' >/dev/null 2>&1 || true
for _ in $(seq 1 20); do curl -fsS "$BASE/healthz" >/dev/null 2>&1 || break; sleep 0.3; done
sleep 0.5
want 1 "$(PGPASSWORD=registry_pw psql --no-psqlrc "$DBURL" -tAc "SELECT count(*) FROM blobs WHERE digest='$KB';")" \
  "before-commit kill: DB row survived (tx rolled back)"
[ -n "$(find data/quarantine -name "$KB" 2>/dev/null)" ] && ok "before-commit kill: bytes held in quarantine" \
  || bad "before-commit kill: no quarantined file"
pkill -f 'registry -addr' 2>/dev/null; sleep 0.5
( cd "$(dirname "$0")/.." && DATABASE_URL="$DBURL" ./bin/registry -addr "$ADDR" -data ./data -faults -blob-grace 0s -recover \
   > "$GEN/server-recover2.log" 2>&1 ) &
for _ in $(seq 1 30); do curl -fsS "$BASE/healthz" >/dev/null 2>&1 && break; sleep 0.3; done
grep -q "row survived crash, file restored from quarantine" "$GEN/server-recover2.log" \
  && ok "before-commit recovery restored the bytes" || bad "restore not logged"
want 200 "$(curl -s -o /dev/null -w '%{http_code}' "$BASE/v2/alpha/blobs/$KB")" "restored blob is readable"
curl -s "$BASE/v2/alpha/blobs/$KB" -o "$GEN/kb.got"
cmp -s "$GEN/crash-before.bin" "$GEN/kb.got" && ok "restored blob bytes are intact" || bad "restored bytes mismatch"

echo
echo "###################################################################"
echo "# 5. ORPHAN TEMP FILES + DIGEST VERIFICATION + AUDIT"
echo "###################################################################"
UUID=$(curl -s -i -X POST "$BASE/v2/alpha/blobs/uploads/" | grep -i '^Docker-Upload-UUID' | tr -d '\r' | awk '{print $2}')
printf 'junk' > "data/tmp/$UUID.orphan"
PGPASSWORD=registry_pw psql --no-psqlrc "$DBURL" -c "UPDATE uploads SET started_at=now()-interval '3 hours' WHERE id='$UUID';" >/dev/null
curl -s -X POST "$BASE/admin/orphans" > "$GEN/orphans.json"
jget "import sys;sys.exit(0 if any('$UUID'==x for x in d['expired_sessions']) else 1)" < "$GEN/orphans.json" \
  && ok "abandoned upload session cleaned separately" || bad "expired session not reported"
[ ! -e "data/tmp/$UUID" ] && [ ! -e "data/tmp/$UUID.orphan" ] && ok "temp files removed" || bad "temp files remain"
echo "real-content" > "$GEN/bad.bin"
WRONG="sha256:$(printf 'tampered' | sha256sum | cut -d' ' -f1)"
curl -s -X POST "$BASE/v2/alpha/blobs/uploads/?digest=$WRONG" --data-binary @"$GEN/bad.bin" \
  | grep -q DIGEST_MISMATCH && ok "wrong digest rejected by real SHA-256 verification" || bad "digest mismatch not rejected"
echo "-- audit event log (auditable retain/delete reasons)"
AUD=$(curl -s "$BASE/admin/audit?limit=300")
echo "$AUD" | jget "
acts={}
for e in d['events']: acts[e['action']]=acts.get(e['action'],0)+1
print('   events:', dict(sorted(acts.items())))"
jgetstr "$AUD" "import sys;sys.exit(0 if any(e['action']=='gc.delete' for e in d['events']) else 1)" \
  && ok "audit log records deletes" || bad "no gc.delete audit events"
jgetstr "$AUD" "import sys;sys.exit(0 if any('recover' in e['action'] for e in d['events']) else 1)" \
  && ok "audit log records crash recovery" || bad "no recovery audit events"

echo
echo "###################################################################"
echo "# SUMMARY"
echo "###################################################################"
echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" = 0 ]
