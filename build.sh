#!/usr/bin/env bash
# Compile main and test sources with the JDK only (no Maven/Gradle, no external deps).
set -euo pipefail
cd "$(dirname "$0")"

rm -rf out
mkdir -p out/classes out/test-classes

echo "==> compiling main sources"
find src/main/java -name '*.java' > out/main-sources.txt
javac -Werror -Xlint:all -d out/classes @out/main-sources.txt

echo "==> compiling test sources"
find src/test/java -name '*.java' > out/test-sources.txt
javac -Werror -Xlint:all -cp out/classes -d out/test-classes @out/test-sources.txt

echo "==> build OK"
