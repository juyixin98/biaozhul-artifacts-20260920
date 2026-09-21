#!/usr/bin/env bash
# ProofCycle end-to-end demo. Requires: running API at $BASE (default
# http://127.0.0.1:8080), curl and jq. Uses the seeded demo accounts.
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:8080}"
FIX="${FIXDIR:-/tmp/pc-fixtures}"
mkdir -p "$FIX"

DANA_TOK="0000000000000000000000000000000000000000000000000000000000000aa1"
PRIYA_TOK="0000000000000000000000000000000000000000000000000000000000000bb2"
REV1_TOK="00000000000000000000000000000000000000000000000000000000000001c3"
REV2_TOK="00000000000000000000000000000000000000000000000000000000000002d4"
DANA=(-H "Authorization: Bearer $DANA_TOK")
PRIYA=(-H "Authorization: Bearer $PRIYA_TOK")
REV1=(-H "Authorization: Bearer $REV1_TOK")
REV2=(-H "Authorization: Bearer $REV2_TOK")

step() { printf '\n=== %s ===\n' "$*"; }
code() { curl -s -o /tmp/pc-body.json -w '%{http_code}' "$@"; }
body() { jq -c . /tmp/pc-body.json; }

# --- fixtures: a tiny PDF and PNG -------------------------------------------
python3 - "$FIX" <<'PY'
import sys, os, struct, zlib
fix = sys.argv[1]
pdf = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj<<>>endobj\n%%EOF\n"
open(os.path.join(fix, "v1.pdf"), "wb").write(pdf)
open(os.path.join(fix, "v2.pdf"), "wb").write(pdf.replace(b"%%EOF", b"% revised v2\n%%EOF\n"))
def chunk(t, d):
    c = t + d
    return struct.pack(">I", len(d)) + c + struct.pack(">I", zlib.crc32(c) & 0xffffffff)
sig = b"\x89PNG\r\n\x1a\n"
ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
idat = chunk(b"IDAT", zlib.compress(b"\x00\xff\xff\xff"))
iend = chunk(b"IEND", b"")
open(os.path.join(fix, "v3.png"), "wb").write(sig + ihdr + idat + iend)
open(os.path.join(fix, "bad.exe"), "wb").write(b"MZ\x90\x00not a pdf or png")
PY

step "health"
curl -s "$BASE/healthz"; echo

step "PM creates job with 2 reviewers (designer dana=1, pm priya=2, rev1=3, rev2=4)"
J=$(code -X POST "$BASE/api/v1/jobs" "${PRIYA[@]}" -H 'Content-Type: application/json' \
  -d '{"title":"Cookie box Q4","description":"350g cookie carton","designer_id":1,"pm_id":2,"reviewer_ids":[3,4]}')
[ "$J" = 201 ] || { echo "expected 201 got $J"; body; exit 1; }
JOB=$(jq '.job.id' /tmp/pc-body.json); body

step "designer uploads version 1 (PDF)"
[ "$(code -X POST "$BASE/api/v1/jobs/$JOB/versions" "${DANA[@]}" -F "file=@$FIX/v1.pdf;filename=cookie-art-v1.pdf")" = 201 ]
body

step "reject non PDF/PNG magic bytes"
[ "$(code -X POST "$BASE/api/v1/jobs/$JOB/revisions" "${DANA[@]}" -F "file=@$FIX/bad.exe")" = 415 ]
body

step "assigned reviewer cannot upload files (member, wrong role -> 403)"
[ "$(code -X POST "$BASE/api/v1/jobs/$JOB/revisions" "${REV1[@]}" -F "file=@$FIX/v2.pdf")" = 403 ]
body

step "reviewer 1 submits all items: one FAIL with a reason"
ITEMS='[{"code":"COLOR","outcome":"pass"},{"code":"BLEED","outcome":"fail","reason":"trim offset 2mm off"},
        {"code":"TYPOGRAPHY","outcome":"pass"},{"code":"RESOLUTION","outcome":"na"},
        {"code":"BARCODE","outcome":"pass"},{"code":"MATERIAL","outcome":"pass"},
        {"code":"REGULATORY","outcome":"pass"},{"code":"FINISHING","outcome":"pass"}]'
[ "$(code -X PUT "$BASE/api/v1/jobs/$JOB/opinions" "${REV1[@]}" -H 'Content-Type: application/json' \
  -d "{\"items\":$ITEMS}")" = 200 ]
body

step "fail without reason is rejected (400)"
[ "$(code -X PUT "$BASE/api/v1/jobs/$JOB/opinions" "${REV2[@]}" -H 'Content-Type: application/json' \
  -d '{"items":[{"code":"COLOR","outcome":"fail"}]}')" = 400 ]
body

step "reviewer 2 completes all items as pass"
[ "$(code -X PUT "$BASE/api/v1/jobs/$JOB/opinions" "${REV2[@]}" -H 'Content-Type: application/json' \
  -d "{\"items\":$(echo "$ITEMS" | sed 's/"outcome":"fail","reason":"trim offset 2mm off"/"outcome":"pass"/g')}")" = 200 ]
body

step "non-assigned reviewer 3 cannot submit opinions (not a member -> 404)"
REV3=(-H "Authorization: Bearer 00000000000000000000000000000000000000000000000000000000000003e5")
[ "$(code -X PUT "$BASE/api/v1/jobs/$JOB/opinions" "${REV3[@]}" -H 'Content-Type: application/json' \
  -d '{"items":[{"code":"COLOR","outcome":"pass"}]}')" = 404 ]
body

step "PM approval blocked by failed item (422)"
[ "$(code -X POST "$BASE/api/v1/jobs/$JOB/approvals" "${PRIYA[@]}")" = 422 ]
body

step "designer cannot approve own job (403)"
[ "$(code -X POST "$BASE/api/v1/jobs/$JOB/approvals" "${DANA[@]}")" = 403 ]
body

step "reviewer 1 changes BLEED to pass but with stale expected version -> 409"
[ "$(code -X PUT "$BASE/api/v1/jobs/$JOB/opinions" "${REV1[@]}" -H 'Content-Type: application/json' \
  -d '{"items":[{"code":"BLEED","outcome":"pass","expected_version":99}]}')" = 409 ]
body

step "reviewer 1 fetches history to learn current version, then fixes BLEED"
curl -s "$BASE/api/v1/jobs/$JOB/versions/1/history" "${REV1[@]}" \
  | jq '[.history.opinions[] | select(.item_code=="BLEED" and .reviewer_name=="rev1") | .version][0]'
[ "$(code -X PUT "$BASE/api/v1/jobs/$JOB/opinions" "${REV1[@]}" -H 'Content-Type: application/json' \
  -d '{"items":[{"code":"BLEED","outcome":"pass","expected_version":1}]}')" = 200 ]
body

step "PM approval now succeeds (201)"
[ "$(code -X POST "$BASE/api/v1/jobs/$JOB/approvals" "${PRIYA[@]}")" = 201 ]
body

step "report for version 1 (text)"
curl -s "$BASE/api/v1/jobs/$JOB/versions/1/report?format=text" "${PRIYA[@]}"

step "second job to show revision flow resets approval basis"
J2=$(code -X POST "$BASE/api/v1/jobs" "${PRIYA[@]}" -H 'Content-Type: application/json' \
  -d '{"title":"Tea tin label","designer_id":1,"pm_id":2,"reviewer_ids":[3,4]}')
[ "$J2" = 201 ]
JOB2=$(jq '.job.id' /tmp/pc-body.json)
[ "$(code -X POST "$BASE/api/v1/jobs/$JOB2/versions" "${DANA[@]}" -F "file=@$FIX/v1.pdf")" = 201 ]
[ "$(code -X PUT "$BASE/api/v1/jobs/$JOB2/opinions" "${REV1[@]}" -H 'Content-Type: application/json' \
  -d '{"items":[{"code":"COLOR","outcome":"pass"}]}')" = 200 ]

step "designer submits revision v2 (PDF) and v3 (PNG)"
[ "$(code -X POST "$BASE/api/v1/jobs/$JOB2/revisions" "${DANA[@]}" -F "file=@$FIX/v2.pdf")" = 201 ]
body
[ "$(code -X POST "$BASE/api/v1/jobs/$JOB2/revisions" "${DANA[@]}" -F "file=@$FIX/v3.png")" = 201 ]

step "old opinions do not carry over: COLOR is pending on v3 -> approval 422"
[ "$(code -X POST "$BASE/api/v1/jobs/$JOB2/approvals" "${PRIYA[@]}")" = 422 ]
body

step "history of superseded v1 retained"
curl -s "$BASE/api/v1/jobs/$JOB2/versions/1/history" "${REV2[@]}" \
  | jq '{version:.history.round.version_number,status:.history.round.status,opinions:(.history.opinions|length)}'

step "unauthorized user cannot see another job or its files (404, no leak)"
REV4=(-H "Authorization: Bearer 00000000000000000000000000000000000000000000000000000000000004f6")
[ "$(code "$BASE/api/v1/jobs/$JOB" "${REV4[@]}")" = 404 ]
body
[ "$(code "$BASE/api/v1/jobs/$JOB/versions/1/download" "${REV4[@]}")" = 404 ]
[ "$(code "$BASE/api/v1/jobs/$JOB/versions/1/report?format=text" "${REV4[@]}")" = 404 ]
[ "$(code "$BASE/api/v1/jobs/$JOB/versions/1/history" "${REV4[@]}")" = 404 ]

step "versioned history listing"
curl -s "$BASE/api/v1/jobs/$JOB2/history" "${PRIYA[@]}" | jq '[.history[].round.version_number]'

echo
echo "DEMO OK"
