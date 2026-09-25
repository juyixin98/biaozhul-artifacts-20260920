#!/usr/bin/env bash
# 一键自动化测试：unittest 全套 + 所有样例三形态对照 + 手工 φ 环。
set -euo pipefail
cd "$(dirname "$0")"

echo "############ 1) unittest 全套 ############"
python3 -m unittest discover -s tests -v

echo
echo "############ 2) 样例程序 raw / ssa / exec 三形态对照 ############"
run() { # file args(逗号分隔字符串)
  local f="$1"; local a="$2"
  echo "--- $f args=[$a]"
  python3 -m ssa_tool "examples/$f" check
  if [ -n "$a" ]; then
    python3 -m ssa_tool "examples/$f" run --args="$a"
  else
    python3 -m ssa_tool "examples/$f" run
  fi
}
run diamond.toy "4"
run diamond.toy "-3"
run loop_sum.toy "10"
run loop_sum.toy "100"
run unreachable.toy "" ""
run unreachable2.toy "1"
run unreachable2.toy "-5"
run nested_branch.toy "7,7"
run nested_branch.toy "-1,7"
run swap_loop.toy "5"

echo
echo "############ 3) 手工 φ 交换环 ############"
PYTHONPATH=. python3 examples/manual_phi_swap.py

echo
echo "全部通过"
