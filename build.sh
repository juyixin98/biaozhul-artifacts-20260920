#!/usr/bin/env bash
# 纯 JDK 构建脚本（无需 Maven/Gradle，无外部依赖）。
# 用法: ./build.sh           编译主代码 + 测试
#       ./build.sh test      编译并运行全部测试
#       ./build.sh clean     清理 build/
set -euo pipefail
cd "$(dirname "$0")"

BUILD=build
CLASSES=$BUILD/classes

action="${1:-compile}"

case "$action" in
  clean)
    rm -rf "$BUILD"
    echo "已清理 $BUILD/"
    exit 0
    ;;
  compile|test)
    ;;
  *)
    echo "未知参数: $action（支持 compile|test|clean）" >&2
    exit 2
    ;;
esac

if ! command -v javac >/dev/null 2>&1; then
  echo "错误: 未找到 javac，请安装 JDK（已在 JDK 21 上验证）" >&2
  exit 1
fi

mkdir -p "$CLASSES"
find src/main/java src/test/java -name '*.java' > "$BUILD/sources.txt"
echo "编译 $(wc -l < "$BUILD/sources.txt") 个源文件..."
javac -d "$CLASSES" @"$BUILD/sources.txt"
echo "编译完成 -> $CLASSES/"

if [ "$action" = "test" ]; then
  echo "运行测试..."
  java -cp "$CLASSES" cep.test.AllTests
fi
