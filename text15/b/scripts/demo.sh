#!/usr/bin/env bash
# ProofCycle end-to-end demo against the Docker image (seeded demo users).
#
#   docker compose up -d --build
#   ./scripts/demo.sh
#
# Walks: create job -> v1 upload -> fail blocks sign-off -> v2 revision ->
# old opinions retained but inert -> all pass -> sign-off -> version history
# -> JSON and Markdown reports.
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:18090}"
API="$BASE_URL/api/v1"
DESIGNER=demo-designer-token
PM=demo-pm-token
R1=demo-reviewer-1-token
R2=demo-reviewer-2-token

say() { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
hdr() { printf '\n--- %s ---\n' "$*"; }

say "health"
curl -fsS "$BASE_URL/healthz"; echo

say "create job (designer; 2 reviewers, 3-item packaging checklist)"
JOB=$(curl -fsS -X POST "$API/jobs" \
  -H "X-API-Token: $DESIGNER" -H "Content-Type: application/json" \
  -d '{
    "name":"Demo Shipper Display SD-42-'$(date +%s)'",
    "pm_id":2,
    "reviewer_ids":[3,4],
    "checklist":[
      {"code":"COLOR-01","description":"Colors within Delta E 3 of Pantone target"},
      {"code":"DIM-02","description":"Die-line dimensions within +/- 1 mm"},
      {"code":"BAR-04","description":"EAN-13 barcode grade at least B"}
    ]}')
echo "$JOB"
JOB_ID=$(echo "$JOB" | sed -n 's/.*"id":\([0-9]*\).*/\1/p')
echo "job id: $JOB_ID"

say "upload v1 PDF"
printf '%%PDF-1.4\ninitial structural proof\n%%%%EOF\n' > /tmp/pc_v1.pdf
curl -fsS -X POST "$API/jobs/$JOB_ID/revisions" -H "X-API-Token: $DESIGNER" \
  -F file=@/tmp/pc_v1.pdf -F file_name=sd-42_v1.pdf; echo

submit_opinions() { # token job version spec(code:verdict:reason)...
  local token="$1" job="$2" version="$3"; shift 3
  python3 - "$API" "$token" "$job" "$version" "$@" <<'PY'
import json,sys,urllib.request,urllib.error
api,token,job,version=sys.argv[1:5]
items=[]
for spec in sys.argv[5:]:
    parts=spec.split(":",2)
    code=parts[0]; verdict=parts[1]
    it={"checklist_code":code,"verdict":verdict,"expected_version":0}
    if len(parts)==3 and parts[2]:
        it["reason"]=parts[2]
    items.append(it)
body=json.dumps({"version":int(version),"items":items}).encode()
req=urllib.request.Request(f"{api}/jobs/{job}/opinions",data=body,method="POST",
    headers={"Content-Type":"application/json","X-API-Token":token})
try:
    print(urllib.request.urlopen(req).read().decode())
except urllib.error.HTTPError as e:
    print(f"HTTP {e.code}: {e.read().decode()}")
PY
}

say "review v1: reviewer1 all pass; reviewer2 fails COLOR-01 with a reason"
submit_opinions "$R1" "$JOB_ID" 1 COLOR-01:pass DIM-02:pass BAR-04:pass
submit_opinions "$R2" "$JOB_ID" 1 COLOR-01:fail:"Cyan Delta E 6.2, retarget required" DIM-02:pass BAR-04:pass

say "PM sign-off v1 (must be blocked: 1 fail + nothing pending missing)"
hdr "POST /signoff"
curl -sS -o /tmp/pc_resp -w "HTTP %{http_code}\n" -X POST "$API/jobs/$JOB_ID/signoff" \
  -H "X-API-Token: $PM"; cat /tmp/pc_resp; echo

say "designer uploads v2 revision"
printf '%%PDF-1.4\nrevised proof with corrected color targets\n%%%%EOF\n' > /tmp/pc_v2.pdf
curl -fsS -X POST "$API/jobs/$JOB_ID/revisions" -H "X-API-Token: $DESIGNER" \
  -F file=@/tmp/pc_v2.pdf -F file_name=sd-42_v2.pdf; echo

say "old v1 opinions are retained but inert"
hdr "submit against superseded v1 (expect 422 version_superseded)"
submit_opinions "$R1" "$JOB_ID" 1 COLOR-01:pass
hdr "sign-off with only v1 opinions (expect 422 signoff_blocked, v2 pending)"
curl -sS -o /tmp/pc_resp -w "HTTP %{http_code}\n" -X POST "$API/jobs/$JOB_ID/signoff" \
  -H "X-API-Token: $PM"; cat /tmp/pc_resp; echo

say "both reviewers complete v2 as pass"
submit_opinions "$R1" "$JOB_ID" 2 COLOR-01:pass DIM-02:pass BAR-04:pass
submit_opinions "$R2" "$JOB_ID" 2 COLOR-01:pass DIM-02:pass BAR-04:pass

say "PM sign-off v2 (expect 200) and duplicate (expect 409)"
curl -sS -X POST "$API/jobs/$JOB_ID/signoff" -H "X-API-Token: $PM"; echo
curl -sS -o /tmp/pc_resp -w "HTTP %{http_code} " -X POST "$API/jobs/$JOB_ID/signoff" -H "X-API-Token: $PM"
cat /tmp/pc_resp; echo

say "per-version history"
hdr "v1 (superseded, prior opinions preserved)"
curl -fsS -H "X-API-Token: $PM" "$API/jobs/$JOB_ID/versions/1"
echo; hdr "v2 (approved)"
curl -fsS -H "X-API-Token: $PM" "$API/jobs/$JOB_ID/versions/2"; echo

say "reports"
hdr "JSON report"
curl -fsS -H "X-API-Token: $PM" "$API/jobs/$JOB_ID/versions/2/report"; echo
hdr "Markdown report (first 28 lines)"
curl -fsS -H "X-API-Token: $PM" "$API/jobs/$JOB_ID/versions/2/report?format=markdown" | head -28

echo
echo "Demo complete: job $JOB_ID approved on version 2."
