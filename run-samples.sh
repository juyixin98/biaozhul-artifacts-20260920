#!/usr/bin/env bash
#
# Builds (if needed) and runs every fixed sample request under samples/,
# writing each response next to the request in samples/outputs/.
#
# Exit code is non-zero if any sample exits unexpectedly (0 for policy
# samples other than the explicit error one, 2 for the error sample).
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

JAR="target/dst-rule-expander-1.0.0.jar"
if [[ ! -f "$JAR" ]]; then
  echo "Building shaded jar ..."
  mvn -q package -DskipTests || { echo "Build failed"; exit 1; }
fi

OUT_DIR="samples/outputs"
mkdir -p "$OUT_DIR"

fail=0
for req in samples/[0-9][0-9]-*.json; do
  base="$(basename "$req" .json)"
  out="$OUT_DIR/${base}.response.json"
  err="$(mktemp)"

  java -jar "$JAR" expand "$req" -o "$out" 2>"$err"
  code=$?
  [[ -s "$err" ]] && cp "$err" "$OUT_DIR/${base}.stderr.txt"
  rm -f "$err"

  # The error-policy sample is expected to exit 2; everything else exits 0.
  if [[ "$base" == *error* ]]; then expected=2; else expected=0; fi

  if [[ $code -eq $expected ]]; then
    printf 'OK   %-32s exit=%d\n' "$base" "$code"
  else
    printf 'FAIL %-32s exit=%d expected=%d\n' "$base" "$code" "$expected"
    fail=1
  fi
done

echo
echo "Time-zone database:"
java -jar "$JAR" tzdb | grep -E 'tzdbVersion|javaVersion'

exit $fail
