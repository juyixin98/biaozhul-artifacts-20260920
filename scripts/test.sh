#!/usr/bin/env bash
# Compile and run the JUnit 5 test suite from the locked standalone jar.
set -euo pipefail
cd "$(dirname "$0")/.."

scripts/resolve-deps.sh
scripts/build.sh

JAR="deps/junit-platform-console-standalone-1.10.2.jar"
mkdir -p build/test-classes
echo "[test] compiling tests"
find src/test/java -name '*.java' | sort > build/test-sources.txt
javac --release 17 -g:none -Werror -Xlint:all \
  -cp "build/classes:$JAR" -d build/test-classes @build/test-sources.txt

echo "[test] running junit"
java -jar "$JAR" execute \
  --class-path "build/classes:build/test-classes" \
  --scan-class-path \
  --details=tree \
  --fail-if-no-tests
