#!/usr/bin/env bash
# 端到端验收脚本：对 examples/ 下每个程序跑 AST / 优化前IR / 优化后IR，
# 比较可观察行为（输出 + 错误分类 + 出错行）。
set -u
cd "$(dirname "$0")"

pass=0; fail=0
for f in examples/0*.l0; do
  out=$(python3 -m constprop.cli optimize "$f" 2>&1)
  if echo "$out" | grep -q "优化保持等价 : True" \
     && echo "$out" | grep -q "未优化IR==AST: True"; then
    echo "PASS  $(basename "$f")"
    pass=$((pass+1))
  else
    echo "FAIL  $(basename "$f")"
    echo "$out" | tail -n 12
    fail=$((fail+1))
  fi
done
echo "----------------------------------------"
echo "examples: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
