#!/usr/bin/env bash
# 编译并用 manual clock 启动服务（默认端口 8080，窗口长度 100，初始时间 100）。
set -euo pipefail
cd "$(dirname "$0")"
OUT=build/classes
mkdir -p build
find src/main/java -name '*.java' > build/main-sources.txt
mkdir -p "$OUT"
javac -d "$OUT" @build/main-sources.txt
exec java -cp "$OUT" com.winquant.server.Main \
  --port "${PORT:-8080}" --window "${WINDOW:-100}" --clock manual --start "${START:-100}"
