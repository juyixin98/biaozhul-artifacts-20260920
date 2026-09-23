#!/usr/bin/env bash
# 一键验收：构建示例 -> 跑全部测试 -> 验证非法模块 -> 变异活动。
# 所有输出同时打印到终端并追加到 RUNLOG.md（由调用方负责重定向）。
set -u
cd "$(dirname "$0")"

ok=0; fail=0
step() { echo; echo "=============================================================="; echo "# $*"; echo "=============================================================="; }
check() { # check <描述> <退出码>
  if [ "$2" -eq 0 ]; then echo "PASS: $1"; ok=$((ok+1));
  else echo "FAIL(exit=$2): $1"; fail=$((fail+1)); fi
}

step "环境"
python3 --version

step "编译所有合法示例"
for f in examples/*.sl; do
  python3 -m slang compile "$f" -o "${f%.sl}.bin" >/dev/null
  check "compile $f" $?
done

step "验证合法示例（应全部通过）"
for f in examples/sum_loop.bin examples/early_return.bin examples/bool_logic.bin examples/uninit_read.bin; do
  python3 -m slang verify "$f" >/dev/null
  check "verify $f" $?
done

step "运行合法示例"
python3 -m slang run examples/sum_loop.sl 2>/dev/null | grep -qx "55"; check "sum_loop 输出 55" $?
python3 -m slang run examples/early_return.sl 2>/dev/null | tr '\n' ' ' | grep -q -- "-1 0 1 120"; check "early_return 输出 -1 0 1 120" $?
python3 -m slang run examples/bool_logic.sl 2>/dev/null | tr '\n' ' ' | grep -q "true false true false 11"; check "bool_logic 输出序列" $?
python3 -m slang run examples/uninit_read.sl 2>/dev/null | tr '\n' ' ' | grep -q "42 7"; check "uninit_read 输出 42 7" $?

step "生成并验证 7 个手工非法模块"
python3 examples/make_bad_modules.py
for f in examples/bad/*.bin; do
  python3 -m slang verify "$f" >/dev/null 2>&1
  [ $? -eq 1 ]   # 验证器对非法模块必须以退出码 1 拒绝
  check "reject $(basename "$f")" $?
done

step "自动化测试套件"
python3 -m unittest discover -s tests >/tmp/test_out.txt 2>&1
tail -3 /tmp/test_out.txt
grep -q "^OK" /tmp/test_out.txt; check "全部 unittest 通过" $?

step "单字节变异（targeted，四个示例）"
for ex in sum_loop early_return bool_logic uninit_read; do
  python3 -m slang mutate "examples/$ex.sl" >/tmp/mut.txt 2>&1
  rc=$?
  # 退出码 0 = 无 invariant_broken/crash；4 = 出现（绝不可）
  check "mutate $ex 无不变量破坏 (rc=0, got $rc)" $rc
  echo "  --- $ex 汇总 ---"; sed -n '2,4p' /tmp/mut.txt | sed 's/^/  /'
done

step "穷尽变异（极小函数，1275 个）"
printf 'fn main() { print 1; }\n' > /tmp/tiny.sl
python3 -m slang mutate /tmp/tiny.sl --strategy exhaustive >/tmp/ex.txt 2>&1
rc=$?
check "exhaustive tiny 无不变量破坏 (rc=0, got $rc)" $rc
sed -n '2,4p' /tmp/ex.txt | sed 's/^/  /'
grep -q "invariant_broken" /tmp/ex.txt && check "exhaustive 出现 invariant_broken" 1 || check "exhaustive 无 invariant_broken 类别" 0

step "结论"
echo "PASS=$ok FAIL=$fail"
[ "$fail" -eq 0 ]
