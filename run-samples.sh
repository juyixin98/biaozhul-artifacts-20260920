#!/usr/bin/env bash
# Run every request in samples/ against the CLI and dump responses to out/.
# The request-error sample (05) is expected to exit 2 and does not fail this run.
set -uo pipefail
cd "$(dirname "$0")"

if [ ! -d target/classes ]; then ./build.sh >/dev/null; fi
mkdir -p out

for f in samples/*.json; do
  name=$(basename "$f" .json)
  echo "=== $name ==="
  java -cp target/classes com.opp16.engine.Main \
       --out "out/${name}.response.json" \
       --export-plan "out/${name}.plan.txt" \
       --export-data "out/${name}.data.json" \
       --export-selection "out/${name}.selection.json" \
       --export-result "out/${name}.result.json" \
       "$f"
  echo "exit=$?"
done
echo "All sample responses written under out/."
