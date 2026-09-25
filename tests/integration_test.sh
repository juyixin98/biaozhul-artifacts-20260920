#!/usr/bin/env bash
# 集成测试：CLI 行为 + 与独立 Python 参考交叉验证。
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN="$ROOT/build/rectunion"
PASS=0
FAIL=0

ok()   { echo "  [PASS] $1"; PASS=$((PASS+1)); }
bad()  { echo "  [FAIL] $1"; FAIL=$((FAIL+1)); }

assert_eq() {
  # $1 实际值, $2 期望值, $3 描述
  if [ "$1" = "$2" ]; then ok "$3 (= $1)"; else bad "$3 (实际 $1, 期望 $2)"; fi
}

echo "== 1. 样例请求 vs 独立参考 =="
for f in "$ROOT"/examples/*.json; do
  out=$("$BIN" "$f")
  rc=$?
  if [ $rc -ne 0 ]; then bad "$(basename "$f") 退出码 $rc: $out"; continue; fi
  valid=$(printf '%s' "$out" | python3 -c 'import sys,json
d=json.load(sys.stdin)
print("yes" if isinstance(d["area"],str) and isinstance(d["perimeter"],str) else "no")' 2>/dev/null)
  if [ "$valid" != "yes" ]; then bad "$(basename "$f") 响应不是预期 JSON: $out"; continue; fi
  if command -v python3 >/dev/null; then
    want=$(python3 "$ROOT/tests/oracle.py" "$f")
    wa=$(printf '%s' "$want" | python3 -c 'import sys,json;print(json.load(sys.stdin)["area"])')
    wp=$(printf '%s' "$want" | python3 -c 'import sys,json;print(json.load(sys.stdin)["perimeter"])')
    ga=$(printf '%s' "$out" | python3 -c 'import sys,json;print(json.load(sys.stdin)["area"])')
    gp=$(printf '%s' "$out" | python3 -c 'import sys,json;print(json.load(sys.stdin)["perimeter"])')
    method=$(printf '%s' "$want" | python3 -c 'import sys,json;print(json.load(sys.stdin)["method"])')
    if [ "$ga" = "$wa" ] && [ "$gp" = "$wp" ]; then
      ok "$(basename "$f"): area=$ga perimeter=$gp (参考: $method)"
    else
      bad "$(basename "$f"): CLI area=$ga perimeter=$gp; 参考 area=$wa perimeter=$wp"
    fi
  else
    ok "$(basename "$f"): $out（无 python3，跳过交叉验证）"
  fi
done

echo "== 2. stdin 输入 =="
out=$(printf '{"rectangles":[{"x1":0,"y1":0,"x2":1,"y2":1}]}' | "$BIN")
assert_eq "$(printf '%s' "$out" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d["area"],d["perimeter"])' 2>/dev/null)" \
          "1 4" "单矩形经 stdin"

echo "== 3. 手工锚点值（不依赖参考实现） =="
out=$("$BIN" "$ROOT/examples/basic_overlap.json")
assert_eq "$(printf '%s' "$out" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d["area"])')" "17" "basic_overlap 面积 17"
assert_eq "$(printf '%s' "$out" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d["perimeter"])')" "20" "basic_overlap 周长 20"

out=$("$BIN" "$ROOT/examples/empty.json")
assert_eq "$(printf '%s' "$out" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d["area"],d["perimeter"])')" \
          "0 0" "空请求面积/周长均为 0"

# 极端坐标：两个半幅矩形在 x=0 贴合，y 范围均为 [0, 2^63-1)。
# 合并后宽 2^64-1、高 2^63-1。
out=$("$BIN" "$ROOT/examples/extreme_coords.json")
read -r wa wp < <(python3 -c 'w=2**64-1;h=2**63-1;print(w*h,2*(w+h))')
gp=$(printf '%s' "$out" | python3 -c 'import sys,json;print(json.load(sys.stdin)["perimeter"])')
ga=$(printf '%s' "$out" | python3 -c 'import sys,json;print(json.load(sys.stdin)["area"])')
assert_eq "$ga" "$wa" "极端坐标面积 (2^64-1)×(2^63-1)"
assert_eq "$gp" "$wp" "极端坐标周长 2×((2^64-1)+(2^63-1))"

echo "== 4. 退化计数 =="
out=$("$BIN" "$ROOT/examples/nested_adjacent_degenerate.json")
assert_eq "$(printf '%s' "$out" | python3 -c 'import sys,json;print(json.load(sys.stdin)["input_count"])')" "9" "输入矩形数 9"
assert_eq "$(printf '%s' "$out" | python3 -c 'import sys,json;print(json.load(sys.stdin)["nondegenerate_count"])')" "7" "非退化矩形数 7"

echo "== 5. 非法请求必须报错且退出码非零 =="
for f in "$ROOT"/examples/errors/*.json; do
  err=$("$BIN" "$f" 2>&1 >/dev/null)
  rc=$?
  valid=$(printf '%s' "$err" | python3 -c 'import sys,json
try:
    d=json.load(sys.stdin)
    print("yes" if isinstance(d.get("error"), str) and d["error"] else "no")
except Exception:
    print("no")' 2>/dev/null)
  if [ $rc -ne 0 ] && [ "$valid" = "yes" ]; then
    short=$(printf '%s' "$err" | python3 -c 'import sys,json;print(json.load(sys.stdin)["error"][:60])' 2>/dev/null)
    ok "$(basename "$f"): 退出码 $rc，错误 $short"
  else
    bad "$(basename "$f"): 未按预期报错 (rc=$rc, valid_json=$valid, out=$err)"
  fi
done

echo "== 6. 文件不存在 =="
err=$("$BIN" /nonexistent/path.json 2>&1 >/dev/null)
rc=$?
valid=$(printf '%s' "$err" | python3 -c 'import sys,json
try:
    d=json.load(sys.stdin)
    print("yes" if isinstance(d.get("error"), str) and d["error"] else "no")
except Exception:
    print("no")' 2>/dev/null)
if [ $rc -eq 2 ] && [ "$valid" = "yes" ]; then
  ok "不存在的文件返回退出码 2 且错误体为合法 JSON"
else
  bad "不存在的文件处理异常 (rc=$rc, valid_json=$valid, out=$err)"
fi

if command -v python3 >/dev/null; then
  echo "== 7. CLI 随机 fuzz（1000 组，逐格参考） =="
  if python3 "$ROOT/tests/cli_fuzz.py" "$BIN" 1000 20260924; then
    PASS=$((PASS+1))
  else
    FAIL=$((FAIL+1))
  fi
fi

echo
if [ $FAIL -eq 0 ]; then
  echo "集成测试全部通过：$PASS 项。"
  exit 0
else
  echo "集成测试 $FAIL 项失败，$PASS 项通过。"
  exit 1
fi
