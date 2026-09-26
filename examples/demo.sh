#!/usr/bin/env bash
# 端到端演示：encode(分段) -> decode -> merge -> inspect。
# 仅用本项目二进制与系统 python3（用它把两次响应里的 base64 拼成 merge 请求）。
set -euo pipefail

CDC="${CDC:-cargo run --quiet --release --}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "== 1) 编码：两个逻辑文件，各自分段 =="
# file A: 显式两段
$CDC --pretty --request-file examples/encode_segments.json > "$TMP/a.json"
cat "$TMP/a.json"

# file B: 普通行数组（含空串 / NULL / Unicode）
$CDC --pretty --request-file examples/encode_rows.json > "$TMP/b.json"
cat "$TMP/b.json"

echo "== 2) 解码 file A，逐行值还原 =="
python3 - "$TMP/a.json" > "$TMP/decode_req.json" <<'PY'
import json,sys
resp=json.load(open(sys.argv[1]))
print(json.dumps({"op":"decode","data_b64":resp["data_b64"]}))
PY
$CDC --pretty --request-file "$TMP/decode_req.json"

echo "== 3) 合并 file A + file B（canonical 全局字典）=="
python3 - "$TMP/a.json" "$TMP/b.json" > "$TMP/merge_req.json" <<'PY'
import json,sys
a=json.load(open(sys.argv[1])); b=json.load(open(sys.argv[2]))
print(json.dumps({"op":"merge","canonical":True,
                  "inputs_b64":[a["data_b64"],b["data_b64"]]}))
PY
$CDC --pretty --request-file "$TMP/merge_req.json" > "$TMP/merged.json"
cat "$TMP/merged.json"

echo "== 4) 解码合并结果：应为 A 后接 B 的全部行，逐行一致 =="
python3 - "$TMP/merged.json" > "$TMP/decode_merged.json" <<'PY'
import json,sys
m=json.load(open(sys.argv[1]))
print(json.dumps({"op":"decode","data_b64":m["data_b64"]}))
PY
$CDC --pretty --request-file "$TMP/decode_merged.json"

echo "== 5) inspect 合并结果结构 =="
python3 - "$TMP/merged.json" > "$TMP/inspect_req.json" <<'PY'
import json,sys
m=json.load(open(sys.argv[1]))
print(json.dumps({"op":"inspect","data_b64":m["data_b64"]}))
PY
$CDC --pretty --request-file "$TMP/inspect_req.json"
