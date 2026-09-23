#!/usr/bin/env bash
# Build, test and package the bitmap-index service. Zero external dependencies:
# only a JDK 17+ toolchain is required (javac/java/jar).
set -euo pipefail
cd "$(dirname "$0")"

JAVAC="${JAVAC:-javac}"
JAVA="${JAVA:-java}"
JAR="${JAR:-jar}"

OUT=build
CLASSES=$OUT/classes
TEST_CLASSES=$OUT/test-classes
JAR_FILE=$OUT/bitmap-index.jar

cmd="${1:-all}"

compile_main() {
  echo ">> compiling main sources"
  mkdir -p "$CLASSES"
  find src/bitmapindex -name '*.java' > "$OUT/main-sources.txt"
  "$JAVAC" -encoding UTF-8 -d "$CLASSES" @"$OUT/main-sources.txt"
}

compile_tests() {
  echo ">> compiling tests"
  mkdir -p "$TEST_CLASSES"
  find src/test -name '*.java' > "$OUT/test-sources.txt"
  "$JAVAC" -encoding UTF-8 -cp "$CLASSES" -d "$TEST_CLASSES" @"$OUT/test-sources.txt"
}

package() {
  echo ">> packaging $JAR_FILE"
  mkdir -p "$OUT"
  "$JAR" --create --file "$JAR_FILE" \
    --main-class bitmapindex.HttpServerMain \
    -C "$CLASSES" .
}

run_tests() {
  echo ">> running tests"
  "$JAVA" -cp "$CLASSES:$TEST_CLASSES" bitmapindex.TestRunner
}

case "$cmd" in
  clean)      rm -rf "$OUT" ;;
  compile)    mkdir -p "$OUT"; compile_main ;;
  test)       mkdir -p "$OUT"; compile_main; compile_tests; run_tests ;;
  package)    mkdir -p "$OUT"; compile_main; package ;;
  demo)       mkdir -p "$OUT"; compile_main; "$JAVA" -cp "$CLASSES" bitmapindex.Demo "${2:-100000}" ;;
  run)        mkdir -p "$OUT"; compile_main; package
              "$JAVA" -jar "$JAR_FILE" "${@:2}" ;;
  all)        mkdir -p "$OUT"; compile_main; compile_tests; package; run_tests ;;
  *) echo "usage: $0 {clean|compile|test|package|demo [rows]|run [--port=..]|all}"; exit 2 ;;
esac
