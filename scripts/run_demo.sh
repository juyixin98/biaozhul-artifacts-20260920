#!/usr/bin/env bash
# 运行 examples/ 下全部请求样例, 原样打印命令与 JSON 响应。
set -u
cd "$(dirname "$0")/.."
BIN=./build/polygon-clip
mkdir -p build
make -s "$BIN" >/dev/null
for f in examples/*.json; do
    echo "================================================================"
    echo "$ $BIN < $f"
    cat "$f"
    echo "---- response ----"
    "$BIN" < "$f"
    echo "(exit code: $?)"
    echo
done
