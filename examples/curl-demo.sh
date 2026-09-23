#!/usr/bin/env bash
# End-to-end demo against `maskcompiler serve`. Run from the repo root:
#   ./examples/curl-demo.sh
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:8080}"

curl -sS "$BASE/health"
echo

curl -sS -X POST "$BASE/v1/rulesets" \
  -H 'Content-Type: application/json' \
  --data "$(python3 -c 'import json; print(json.dumps({"name": "demo", "ruleset": json.load(open("examples/rules.json"))}))')"
echo

curl -sS -X POST "$BASE/v1/rulesets/demo/apply" \
  -H 'Content-Type: application/json' \
  --data "$(python3 -c 'import json; print(json.dumps({"document": json.load(open("examples/document.json"))}))')"
echo
