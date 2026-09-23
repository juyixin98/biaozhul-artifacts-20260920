#!/usr/bin/env bash
# Run the acceptance experiments (skew, crafted collisions, merge rejection,
# candidate coverage) and capture the report under docs/.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p build docs
rm -rf build/classes build/test-classes
mkdir -p build/classes build/test-classes
find src -name '*.java' > build/sources.txt
find test -name '*.java' > build/test-sources.txt
javac -d build/classes @build/sources.txt
javac -cp build/classes -d build/test-classes @build/test-sources.txt
{
  echo "# Acceptance experiment output"
  echo "# Generated: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "# Command: java -cp build/classes:build/test-classes test.exp.AcceptanceExperiments"
  echo
  java -cp build/classes:build/test-classes test.exp.AcceptanceExperiments
} 2>&1 | tee docs/acceptance-output.txt
