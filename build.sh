#!/usr/bin/env bash
# Build and test the diff service with nothing but a JDK (no Maven/Gradle/deps).
set -euo pipefail
cd "$(dirname "$0")"

OUT=build/classes
rm -rf build
mkdir -p "$OUT"

echo ">> compiling..."
find src tests -name '*.java' > build/sources.txt
javac -d "$OUT" @build/sources.txt

echo ">> running tests..."
java -cp "$OUT" com.example.diff.RunAllTests

echo ">> build OK"
