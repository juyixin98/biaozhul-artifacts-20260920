#!/usr/bin/env bash
# touch_marker.sh — writes a canary file into its working directory so tests
# can prove fixture cwd is the per-execution work directory, not the cache.
set -euo pipefail
printf 'cwd=%s\n' "$PWD"
touch "canary-${1:-default}.txt"
echo "wrote canary in working directory"
