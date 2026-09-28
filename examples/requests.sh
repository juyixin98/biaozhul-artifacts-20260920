#!/usr/bin/env bash
# Ready-made requests for a locally running service.
# Start it first with:
#   RC_LOG_DIR=./logs python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8000
set -u
BASE="${BASE:-http://127.0.0.1:8000}"

post() { curl -s -X POST "$BASE/certify" -H 'Content-Type: application/json' -d "$1"; }

echo "== 1. simple root (sqrt(2)), certified by interval-Newton uniqueness =="
post '{"expression":"x^2 - 2","lower":"1","upper":"2","include_approximation":false}'

echo; echo "== 2. no real root =="
post '{"expression":"x^2 + 1","lower":"-2","upper":"2","include_approximation":false}'

echo; echo "== 3. double root -> undecided (never mis-certified) =="
post '{"expression":"x^2","lower":"-1","upper":"1","include_approximation":false}'

echo; echo "== 4. multiple roots: sin(x) on [0, 2pi] =="
post '{"expression":"sin(x)","lower":"0","upper":"6.2832","include_approximation":false}'

echo; echo "== 5. very-near-root boundary, still isolated and tight =="
post '{"expression":"x^2 - 2","lower":"0","upper":"1.4142135623730951","include_approximation":false}'

echo; echo "== 6. root exactly on the boundary (monotone IVT) =="
post '{"expression":"x","lower":"0","upper":"1","include_approximation":false}'

echo; echo "== 7. certified roots plus the separate UNVERIFIED approximation =="
post '{"expression":"x^2 - 2","lower":"1","upper":"2","include_approximation":true}'

echo; echo "== 8. input parse error (HTTP 400, with source position) =="
post '{"expression":"2x","lower":"0","upper":"1"}'

echo; echo "== 9. state conflict: lower >= upper (HTTP 409) =="
post '{"expression":"x","lower":"2","upper":"1"}'

echo; echo "== 10. domain error with position (HTTP 422) =="
post '{"expression":"sqrt(x-1)","lower":"0","upper":"2"}'

echo; echo "== 11. resource exhaustion -> HTTP 200 with partial results =="
post '{"expression":"sin(x)","lower":"0","upper":"100","max_evals":40,"include_approximation":false}'
echo
