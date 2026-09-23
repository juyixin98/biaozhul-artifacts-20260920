#!/usr/bin/env bash
# Compile all main sources with the JDK's javac (no external dependencies).
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p build/classes
find src -name '*.java' > build/sources.txt
javac -d build/classes @build/sources.txt
echo "Built main sources into build/classes"
