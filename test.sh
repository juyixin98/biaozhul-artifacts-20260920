#!/usr/bin/env bash
# 编译并运行全部 JUnit 5 测试。
# 首次运行若 lib/ 下缺少 junit standalone jar，会自动从 Maven Central 下载。
set -euo pipefail
cd "$(dirname "$0")"

JUNIT_JAR=lib/junit-platform-console-standalone-1.11.4.jar
JUNIT_URL=https://repo.maven.apache.org/maven2/org/junit/platform/junit-platform-console-standalone/1.11.4/junit-platform-console-standalone-1.11.4.jar

if [[ ! -f "$JUNIT_JAR" ]]; then
  echo "未找到 $JUNIT_JAR，开始下载……"
  mkdir -p lib
  if command -v curl >/dev/null 2>&1; then
    curl -sSLf -o "$JUNIT_JAR" "$JUNIT_URL"
  else
    wget -q -O "$JUNIT_JAR" "$JUNIT_URL"
  fi
fi

./build.sh

mkdir -p target/test-classes
find src/test/java -name '*.java' > target/test-sources.txt
javac -d target/test-classes \
  -cp "target/classes:$JUNIT_JAR" --release 21 @target/test-sources.txt
echo "测试代码编译完成，开始执行……"
exec java -jar "$JUNIT_JAR" execute \
  -cp target/classes -cp target/test-classes \
  --scan-classpath --details=tree
