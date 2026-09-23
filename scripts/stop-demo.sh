#!/usr/bin/env bash
set -u
for name in stub coord a b; do
  f="/tmp/relay-demo-${name}.pid"
  if [ -f "$f" ]; then
    kill "$(cat "$f")" 2>/dev/null || true
    rm -f "$f"
  fi
done
echo "demo services stopped"
