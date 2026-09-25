#!/usr/bin/env bash
# Compile all production sources into build/classes using only the JDK.
set -euo pipefail
cd "$(dirname "$0")"
rm -rf build
mkdir -p build/classes
find src -name '*.java' > build/sources.txt
javac -d build/classes @build/sources.txt
echo "compiled $(wc -l < build/sources.txt) source files -> build/classes"
