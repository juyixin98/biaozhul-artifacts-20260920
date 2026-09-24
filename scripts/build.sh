#!/usr/bin/env bash
# Zero-dependency build: compiles all sources with javac and packages an
# executable JAR. Requires JDK 17+ (developed/tested on JDK 21).
set -euo pipefail
cd "$(dirname "$0")/.."

OUT=build
rm -rf "$OUT"
mkdir -p "$OUT/classes" "$OUT/test-classes" "$OUT/jar"

echo "[1/3] compiling main sources"
find src -name '*.java' | sort > "$OUT/sources.txt"
javac -d "$OUT/classes" @"$OUT/sources.txt"

echo "[2/3] compiling tests"
find test -name '*.java' | sort > "$OUT/test-sources.txt"
javac -cp "$OUT/classes" -d "$OUT/test-classes" @"$OUT/test-sources.txt"

echo "[3/3] packaging jar"
jar --create --file "$OUT/jar/position-diff.jar" \
    --main-class com.example.positiondiff.Main -C "$OUT/classes" .

echo "OK -> $OUT/jar/position-diff.jar"
