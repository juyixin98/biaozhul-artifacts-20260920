#!/usr/bin/env bash
# Zero-dependency build: compiles all main sources with plain javac.
# Requires JDK 17+ (developed/tested on JDK 21).
set -euo pipefail
cd "$(dirname "$0")"

OUT=build/classes
mkdir -p "$OUT"

echo "[build] javac main sources"
find src/main/java -name '*.java' | sort > build/main-sources.txt
javac -d "$OUT" @build/main-sources.txt

echo "[build] OK -> $OUT"
