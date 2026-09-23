#!/usr/bin/env bash
# Resolve locked test-only dependency: verify checksum, download if missing.
# The runtime service itself has zero third-party dependencies.
set -euo pipefail
cd "$(dirname "$0")/.."

JAR="deps/junit-platform-console-standalone-1.10.2.jar"
EXPECTED="a1de557821293ce903c213c694165fff532cf92081bac4238b9e05b35f04f43f"
URL="https://repo1.maven.org/maven2/org/junit/platform/junit-platform-console-standalone/1.10.2/junit-platform-console-standalone-1.10.2.jar"

if [[ -f "$JAR" ]] && echo "$EXPECTED  $JAR" | sha256sum -c --status; then
  echo "[deps] $JAR present, checksum OK"
  exit 0
fi

if [[ -f "$JAR" ]]; then
  echo "[deps] checksum MISMATCH for $JAR; removing and re-downloading" >&2
  rm -f "$JAR"
fi

echo "[deps] downloading $URL"
curl -fsSL --retry 3 -o "$JAR.tmp" "$URL"
if ! echo "$EXPECTED  $JAR.tmp" | sha256sum -c --status; then
  echo "[deps] downloaded jar failed checksum verification" >&2
  rm -f "$JAR.tmp"
  exit 1
fi
mv "$JAR.tmp" "$JAR"
echo "[deps] OK: $JAR"
