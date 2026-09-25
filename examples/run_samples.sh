#!/usr/bin/env bash
# License-expression judge — request samples.
#
# Runs entirely against a local server started with:
#   go run ./cmd/licensejudge
# Requires: curl, a running server on 127.0.0.1:8080 (override BASE_URL).
# No external/network service is contacted.
set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BASE_URL="${BASE_URL:-http://127.0.0.1:8080}"
OUT_DIR="${OUT_DIR:-$SCRIPT_DIR/responses}"
REQ_DIR="$SCRIPT_DIR/requests"
mkdir -p "$OUT_DIR"

post() { # $1 = name, $2 = path, $3 = json file (under requests/)
  local name="$1" path="$2" body="$REQ_DIR/$3"
  echo "==> $name  POST $path"
  curl -sS -X POST "$BASE_URL$path" \
    -H 'Content-Type: application/json' \
    --data-binary "@$body" \
    -o "$OUT_DIR/$name.json" -w 'HTTP %{http_code}\n'
}

get() { # $1 = name, $2 = path
  local name="$1" path="$2"
  echo "==> $name  GET $path"
  curl -sS "$BASE_URL$path" -o "$OUT_DIR/$name.json" -w 'HTTP %{http_code}\n'
}

get  health                 /healthz
get  policy                 /v1/policy
get  fixtures               /v1/fixtures

post judge-allow-or         /v1/judge judge-allow-or.json
post judge-allow-branch     /v1/judge judge-allow-branch.json
post judge-precedence       /v1/judge judge-precedence.json
post judge-parentheses      /v1/judge judge-parentheses.json
post judge-with-allow       /v1/judge judge-with-allow.json
post judge-with-deny-combo  /v1/judge judge-with-deny-combo.json
post judge-denied-all       /v1/judge judge-denied-all.json
post judge-unknown          /v1/judge judge-unknown.json
post judge-invalid          /v1/judge judge-invalid.json
post fixture-run-go-version /v1/fixtures/run fixture-go-version.json
post fixture-run-not-allowed /v1/fixtures/run fixture-not-allowed.json

echo
echo "Responses written to: $OUT_DIR"
