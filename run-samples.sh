#!/usr/bin/env bash
# 依次离线运行 samples/ 下全部请求样例，输出写入 samples/out/。
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d build ]; then
  ./build.sh >/dev/null
fi

mkdir -p samples/out
for req in samples/*.json; do
  out="samples/out/$(basename "${req%.json}").response.json"
  java -cp build sessions.Main run "$req" > "$out"
  echo "ran $req -> $out"
done
