#!/usr/bin/env bash
# 编译并运行全部自动化测试。仅需 JDK 17+，无第三方依赖。
set -euo pipefail
cd "$(dirname "$0")/.."

JAVA_HOME="${JAVA_HOME:-$HOME/jdk17/usr/lib/jvm/java-17-openjdk-amd64}"
JAVAC="$JAVA_HOME/bin/javac"; JAVA="$JAVA_HOME/bin/java"
[ -x "$JAVAC" ] || JAVAC="$(command -v javac)"
[ -x "$JAVA" ] || JAVA="$(command -v java)"

rm -rf out test-classes
mkdir -p out test-classes

echo "== 编译主源码 =="
"$JAVAC" -encoding UTF-8 -d out $(find src -name '*.java')

echo "== 编译测试 =="
"$JAVAC" -encoding UTF-8 -cp out -d test-classes $(find tests -name '*.java')

echo
echo "############################################################"
echo "# 1/2 进程内单元/集成测试"
echo "############################################################"
"$JAVA" -cp out:test-classes com.example.txflow.InProcessTest
UNIT_RC=$?

echo
echo "############################################################"
echo "# 2/2 崩溃恢复端到端测试（真实子进程 + Runtime.halt 注入）"
echo "############################################################"
set +e
"$JAVA" -cp out:test-classes com.example.txflow.CrashRecoveryTest
CRASH_RC=$?
set -e

echo
if [ "$UNIT_RC" -eq 0 ] && [ "$CRASH_RC" -eq 0 ]; then
  echo "全部测试通过 ✅"
  exit 0
else
  echo "存在失败：unit=$UNIT_RC crash=$CRASH_RC ❌"
  exit 1
fi
