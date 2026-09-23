#!/usr/bin/env bash
# 依次运行 samples/ 下全部请求，把响应 JSON 写到 build/responses/，
# 并在终端打印每个样本的摘要、计划文本与穷举校验结果。
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -d build/classes ]; then ./scripts/build.sh >/dev/null; fi
mkdir -p build/responses
python3 scripts/gen_samples.py >/dev/null

for f in samples/*.json; do
  name=$(basename "$f" .json)
  out="build/responses/$name.json"
  echo "================================================================"
  echo "样本: $f"
  java -cp build/classes joinopt.Main "$f" --export-dir "build/out/$name" > "$out"
  python3 - "$out" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
print("摘要:", json.dumps(d["summary"], ensure_ascii=False))
if "enumeration" in d:
    print("穷举:", json.dumps(d["enumeration"], ensure_ascii=False))
if "result" in d:
    print("估计/实际:", json.dumps(d["result"]["estimateVsActual"], ensure_ascii=False))
print("计划:")
print(d["planText"])
for w in d.get("warnings", []):
    print("注意:", w)
PY
done
echo "================================================================"
echo "全部响应已写入 build/responses/，导出件在 build/out/<样本名>/"
