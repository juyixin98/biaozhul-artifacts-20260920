#!/usr/bin/env bash
# Run the acceptance demo: exact-vs-approx error stats, collision, merge rejection.
set -euo pipefail
cd "$(dirname "$0")"
./build.sh
java -cp build/classes approxheavy.demo.AccuracyDemo "${1:-42}"
