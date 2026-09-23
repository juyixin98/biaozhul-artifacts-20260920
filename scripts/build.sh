#!/usr/bin/env bash
# Compile main sources with the JDK only (no third-party dependencies).
set -euo pipefail
cd "$(dirname "$0")/.."
rm -rf build/classes
mkdir -p build/classes build/test-classes
find src/main/java -name '*.java' > build/main-sources.txt
javac -Xlint:all -Werror -d build/classes @build/main-sources.txt
echo "main compiled -> build/classes"
