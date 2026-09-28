#!/usr/bin/env bash
#
# Local demo: boots the real server on an ephemeral DB and drives the four
# required cases over HTTP, then prints the replayable event stream.
# Requires: npm install has been run.
set -euo pipefail

cd "$(dirname "$0")/.."

if [ ! -d node_modules ]; then
  echo "node_modules missing — run 'npm install' first" >&2
  exit 1
fi

node scripts/demo.mjs
