#!/usr/bin/env bash
# 零外部构建工具的构建+测试脚本：只需 JDK 17+（javac/java）。
# 首次运行自动下载 junit-platform-console-standalone（单 jar）到 lib/。
set -euo pipefail
cd "$(dirname "$0")"

LIB_DIR=lib
JUNIT_JAR="$LIB_DIR/junit-platform-console-standalone.jar"
JUNIT_URLS=(
  "https://repo1.maven.org/maven2/org/junit/platform/junit-platform-console-standalone/1.11.4/junit-platform-console-standalone-1.11.4.jar"
  "https://repo1.maven.org/maven2/org/junit/platform/junit-platform-console-standalone/1.10.5/junit-platform-console-standalone-1.10.5.jar"
)

if [[ ! -f "$JUNIT_JAR" ]]; then
  mkdir -p "$LIB_DIR"
  echo ">> downloading junit-platform-console-standalone ..."
  for url in "${JUNIT_URLS[@]}"; do
    if curl -fsSL --retry 2 -o "$JUNIT_JAR" "$url"; then
      echo ">> downloaded: $url"
      break
    fi
  done
fi
[[ -s "$JUNIT_JAR" ]] || { echo "ERROR: failed to fetch JUnit jar" >&2; exit 1; }

OUT=build/classes
TEST_OUT=build/test-classes
rm -rf build
mkdir -p "$OUT" "$TEST_OUT"

echo ">> compiling main sources"
find src/main/java -name '*.java' > build/main-sources.txt
javac -d "$OUT" @build/main-sources.txt

echo ">> compiling tests"
find src/test/java -name '*.java' > build/test-sources.txt
javac -d "$TEST_OUT" -cp "$JUNIT_JAR:$OUT" @build/test-sources.txt

echo ">> running tests"
java -jar "$JUNIT_JAR" execute \
  --class-path "$TEST_OUT:$OUT" \
  --scan-class-path \
  --details=tree \
  --fail-if-no-tests

echo ">> BUILD OK"
