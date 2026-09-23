#!/usr/bin/env bash
# Build main sources (zero third-party dependencies) into build/classes.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "[build] javac (release 17)"
mkdir -p build/classes build/dist
find src/main/java -name '*.java' | sort > build/sources.txt
javac --release 17 -g:none -Werror -Xlint:all \
  -d build/classes @build/sources.txt
echo "[build] OK -> build/classes"
