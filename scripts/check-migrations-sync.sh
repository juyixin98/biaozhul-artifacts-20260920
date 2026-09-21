#!/usr/bin/env bash
# Fail if the canonical migrations (db/migrations) and the embedded copies the
# Go binary ships (internal/db/migrations) drift apart. Run in CI.
set -euo pipefail
cd "$(dirname "$0")/.."
rc=0
for f in db/migrations/*.sql; do
  b=$(basename "$f")
  if ! diff -q "$f" "internal/db/migrations/$b" >/dev/null; then
    echo "DRIFT: $b — copy db/migrations/$b to internal/db/migrations/$b"
    rc=1
  fi
done
# New files added in canonical dir but missing from embed dir.
for f in db/migrations/*.sql; do
  b=$(basename "$f")
  if [ ! -f "internal/db/migrations/$b" ]; then
    echo "MISSING in embed: $b"
    rc=1
  fi
done
if [ "$rc" -eq 0 ]; then
  echo "migrations in sync"
fi
exit $rc
