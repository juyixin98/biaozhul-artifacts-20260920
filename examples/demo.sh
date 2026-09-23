#!/usr/bin/env bash
# 端到端演示：扫描 -> 修改 -> 增量分析 -> 删除 -> 增量分析。
# 用法: bash examples/demo.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_REPO="$HERE/.."
PROJ="$ROOT_REPO/testdata/demo"
WORK="$(mktemp -d)"
CACHE="$WORK/cache"
trap 'rm -rf "$WORK"' EXIT

# 复制夹具到临时工作目录，演示中的删除不会污染仓库
cp -r "$PROJ" "$WORK/demo"
DEMO="$WORK/demo"

echo "================ 构建 ================"
go -C "$ROOT_REPO" build -o "$WORK/depscanner" ./cmd/depscanner

echo
echo "================ 1) 首次扫描（建立基线） ================"
"$WORK/depscanner" scan \
  --root "$DEMO" --cache-dir "$CACHE" \
  --targets app/main.c --targets edit/main_edit.c \
  --system-dirs sysinc \
  | tee "$WORK/scan1.json" | head -60
echo "..."

echo
echo "================ 2) 修改共享头 edit/hdr/version.h ================"
echo '#define VERSION_STRING "2.0.0-CHANGED"' >> "$DEMO/edit/hdr/version.h"
"$WORK/depscanner" affected \
  --root "$DEMO" --cache-dir "$CACHE" \
  --targets app/main.c --targets edit/main_edit.c \
  --system-dirs sysinc \
  | tee "$WORK/affected1.json" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print("changed:", d["changed"]); print("affected_targets:", d["affected_targets"])'

echo
echo "================ 3) 删除 edit/hdr/version.h ================"
rm "$DEMO/edit/hdr/version.h"
"$WORK/depscanner" affected \
  --root "$DEMO" --cache-dir "$CACHE" \
  --targets app/main.c --targets edit/main_edit.c \
  --system-dirs sysinc \
  | tee "$WORK/affected2.json" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print("changed:", d["changed"]); print("affected_targets:", d["affected_targets"]); print("warnings:", [x["message"] for x in d["diagnostics"]])'

echo
echo "================ 4) 宏生成 include 必须报错 ================"
"$WORK/depscanner" scan \
  --root "$DEMO" --cache-dir "$CACHE" \
  --targets bad/macro_include.c && echo "ERROR: 应当失败" || echo "→ 如预期返回非零退出码"

echo
echo "================ 5) 循环引用检测（不更新基线） ================"
"$WORK/depscanner" scan \
  --root "$DEMO" --cache-dir "$CACHE" \
  --targets cycle/main_cycle.c \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print("cycles:", d["graph"]["cycles"])'

echo
echo "演示完成（临时目录 $WORK 将被清理）"
