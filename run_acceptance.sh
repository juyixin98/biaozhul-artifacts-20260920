#!/usr/bin/env bash
# 一键复现三个验收点。用法：bash run_acceptance.sh
set -u
cd "$(dirname "$0")"

pass=0; fail=0
check() { # desc expected_substring actual
  if printf '%s' "$3" | grep -q "$2"; then
    echo "  [PASS] $1"; pass=$((pass+1))
  else
    echo "  [FAIL] $1（未在输出中找到: $2）"; fail=$((fail+1))
  fi
}

echo "############ 验收一：恒等函数多次实例化 ############"
out1=$(python3 -m tinyinfer.cli infer examples/identity.tml 2>&1)
echo "$out1" | sed 's/^/  /'
check "最终类型为 int" "最终表达式类型: int" "$out1"
check "id 是多态方案" "forall 'a. 'a -> 'a" "$out1"
check "求值结果为 8" "求值结果: 8" "$out1"

echo
echo "############ 验收二：递归类型被 occurs-check 拒绝 ############"
out2=$(python3 -m tinyinfer.cli infer examples/recursive_type_reject.tml 2>&1)
rc2=$?
echo "$out2" | sed 's/^/  /'
check "退出码非 0" "" "$([ $rc2 -ne 0 ] && echo OK)"
check "错误为 OccursError" "OccursError" "$out2"
check "报告递归类型" "递归" "$out2"
check "给出冲突位置" "(4:23)" "$out2"

echo
echo "############ 验收三a：值限制开启 —— 静态拒绝 ############"
out3a=$(python3 -m tinyinfer.cli infer examples/ref_unsoundness.tml 2>&1)
rc3a=$?
echo "$out3a" | sed 's/^/  /'
check "退出码非 0" "" "$([ $rc3a -ne 0 ] && echo OK)"
check "错误为 UnifyError" "UnifyError" "$out3a"
check "冲突含 int/bool" "期望 bool，实际得到 int" "$out3a"

echo
echo "############ 验收三b：naive 无值限制 —— 通过但运行期崩溃 ############"
out3b=$(python3 -m tinyinfer.cli infer examples/ref_unsoundness.tml --naive 2>&1)
rc3b=$?
echo "$out3b" | sed 's/^/  /'
check "类型检查通过(退出码 0)" "" "$([ $rc3b -eq 0 ] && echo OK)"
check "r 被一般化为多态" "forall 'a. 'a ref" "$out3b"
check "运行期崩溃暴露不健全" "bool(True) + 1" "$out3b"

echo
echo "############ 自动化测试套件 ############"
if python3 -m unittest discover -s tests >/tmp/tinyinfer_tests.log 2>&1; then
  echo "  [PASS] 全部单元测试通过"; pass=$((pass+1))
  grep -E "^Ran" /tmp/tinyinfer_tests.log | sed 's/^/  /'
else
  echo "  [FAIL] 单元测试存在失败"; fail=$((fail+1))
  tail -20 /tmp/tinyinfer_tests.log | sed 's/^/  /'
fi

echo
echo "=================================================="
echo "结果：PASS=$pass FAIL=$fail"
[ "$fail" -eq 0 ]
