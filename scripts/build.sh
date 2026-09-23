#!/usr/bin/env bash
# Compile all main sources with plain javac (no external dependencies, JDK 17+).
set -euo pipefail
cd "$(dirname "$0")/.."
rm -rf out/classes
mkdir -p out/classes
find src/main/java -name '*.java' > out/main-sources.txt
javac -d out/classes @out/main-sources.txt
echo "Main classes compiled to out/classes"
