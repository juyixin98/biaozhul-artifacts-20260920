#!/usr/bin/env bash
# 编译并运行全部自动化测试。仅依赖 JDK（无第三方库）。
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v javac >/dev/null 2>&1; then
  if [ -x "$HOME/jdk/bin/javac" ]; then
    export PATH="$HOME/jdk/bin:$PATH"
  else
    echo "ERROR: 未找到 javac，请安装 JDK 17+ 或把 JDK 解压到 ~/jdk" >&2
    exit 1
  fi
fi

echo "javac: $(javac -version 2>&1)"
rm -rf build
mkdir -p build/classes build/test-classes

javac -encoding UTF-8 -d build/classes $(find src -name '*.java')
javac -encoding UTF-8 -cp build/classes -d build/test-classes $(find test -name '*.java')

java -cp build/classes:build/test-classes topk.TestMain
