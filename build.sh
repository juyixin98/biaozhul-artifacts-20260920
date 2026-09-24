#!/usr/bin/env bash
# Compile main and test sources with the JDK javac only (no dependencies).
set -euo pipefail
cd "$(dirname "$0")"
rm -rf out
mkdir -p out
find src test -name '*.java' > build/sources.txt
javac -d out @build/sources.txt
echo "compiled -> out/"
