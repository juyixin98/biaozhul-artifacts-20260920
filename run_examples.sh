#!/usr/bin/env bash
# 批量运行 examples/ 下全部请求样例，结果写入 examples/out/。
set -euo pipefail
cd "$(dirname "$0")"

go build -o bin/snapshotsim ./cmd/snapshotsim
mkdir -p examples/out
for f in examples/*.json; do
  name="$(basename "$f" .json)"
  echo "== 运行 $name =="
  ./bin/snapshotsim -in "$f" -out "examples/out/${name}.result.json"
  echo "   -> examples/out/${name}.result.json"
done
echo "全部样例运行完成。"
