#!/usr/bin/env bash
# 编译并运行全部自动化测试（零依赖，JDK 自带工具）。
# 退出码 0 = 全部通过。
set -euo pipefail
cd "$(dirname "$0")"
JAVA_HOME_DIR="${JAVA_HOME:-}"
JAVAC="${JAVAC:-javac}"
JAVA="${JAVA:-java}"

echo ">> 编译主程序..."
rm -rf build/classes
mkdir -p build/classes
"$JAVAC" --release 17 -d build/classes $(find src/main -name '*.java')

echo ">> 编译测试..."
rm -rf build/test
mkdir -p build/test
"$JAVAC" --release 17 -d build/test -cp build/classes $(find src/test -name '*.java')

rc=0
echo ">> 运行核心测试 incagg.StoreTest"
if ! "$JAVA" -cp build/classes:build/test incagg.StoreTest; then rc=1; fi
echo
echo ">> 运行 HTTP 端到端冒烟 incagg.web.HttpSmokeTest"
if ! "$JAVA" -cp build/classes:build/test incagg.web.HttpSmokeTest; then rc=1; fi

echo
if [ $rc -eq 0 ]; then
  echo "全部测试通过 ✔"
else
  echo "存在失败项，见上方输出"
fi
exit $rc
