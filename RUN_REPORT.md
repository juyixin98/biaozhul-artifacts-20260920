# 运行记录（实际执行于 2026-09-23 20:09:46 CST）

环境：Python 3.12.3，Linux 6.8.0-90-generic x86_64

## 未通过项 / 已知边界（如实记录）

* **无未通过测试**：最终全套 58 个 unittest、10 组样例 × 三形态对照、
  手工 φ 二环/三环、HTTP 冒烟全部通过。
* 开发过程中真实出现并修复的语义 bug（均已被回归测试固化）：
  1. 解析器 dataclass 位置参数错位（`Binary(op,left,right,span)` 传成
     `(op,left,right)`）——改为关键字构造；
  2. `&&`/`||` 短路左分支未写结果槽；
  3. φ 名与 alloc 名计数器不共享导致“重复定义”，以及
     φ 入边一度填成“copy 的静态源”而非槽版本名（后者使
     `t=a;a=b;b=t` 交换循环三形态不一致）——这是最实质的 SSA bug；
  4. 并行复制串行化经历三次错误实现（环外读旧值被提前覆盖），
     最终版通过 100 万组随机并行复制等价性检验；
  5. 循环体内 `var j=0;` 初值只在入口写一次，导致嵌套循环计数错误；
  6. φ 消除最初直接覆盖 φ 目的名，破坏循环体内的 SSA 使用——改为
     “φ 目的位置化（r<N>）+ 全函数 use 改写”。
* 设计边界（非缺陷）：单函数、无函数调用/堆；不做 SSA 之上的优化；
  负数命令行参数须写成 `--args=-1`；解释器有步数上限。

## 1. 自动化测试：python3 -m unittest discover -s tests
```
..........................................................
----------------------------------------------------------------------
Ran 58 tests in 4.225s

OK
```

## 2. 一键脚本 bash run_tests.sh（样例三形态对照 + 手工 φ 环）
```
############ 1) unittest 全套 ############
test_diamond (test_dominance.TestDominance.test_diamond) ... ok
test_loop (test_dominance.TestDominance.test_loop) ... ok
test_unreachable_excluded (test_dominance.TestDominance.test_unreachable_excluded) ... ok
test_swap_cycle (test_manual_phi_cycles.TestManualPhiSwap.test_swap_cycle) ... ok
test_three_cycle_phi (test_manual_phi_cycles.TestManualPhiSwap.test_three_cycle_phi) ... ok
test_cycle3 (test_parallel_copy.TestParallelCopy.test_cycle3) ... ok
test_cycle_with_outsider (test_parallel_copy.TestParallelCopy.test_cycle_with_outsider) ... ok
test_plain (test_parallel_copy.TestParallelCopy.test_plain) ... ok
test_random_equivalence (test_parallel_copy.TestParallelCopy.test_random_equivalence) ... ok
test_self_and_const (test_parallel_copy.TestParallelCopy.test_self_and_const) ... ok
test_swap2 (test_parallel_copy.TestParallelCopy.test_swap2) ... ok
test_temp_required (test_parallel_copy.TestParallelCopy.test_temp_required) ... ok
test_two_cycles (test_parallel_copy.TestParallelCopy.test_two_cycles) ... ok
test_bad_char (test_parser.TestLexer.test_bad_char) ... ok
test_block_comment (test_parser.TestLexer.test_block_comment) ... ok
test_line_comment (test_parser.TestLexer.test_line_comment) ... ok
test_line_tracking (test_parser.TestLexer.test_line_tracking) ... ok
test_token_positions (test_parser.TestLexer.test_token_positions) ... ok
test_unterminated_comment (test_parser.TestLexer.test_unterminated_comment) ... ok
test_duplicate_decl (test_parser.TestParser.test_duplicate_decl) ... ok
test_if_else_if_chain (test_parser.TestParser.test_if_else_if_chain) ... ok
test_main_only (test_parser.TestParser.test_main_only) ... ok
test_missing_semicolon (test_parser.TestParser.test_missing_semicolon) ... ok
test_parses (test_parser.TestParser.test_parses) ... ok
test_precedence (test_parser.TestParser.test_precedence) ... ok
test_span_roundtrip (test_parser.TestParser.test_span_roundtrip) ... ok
test_unary_and_comparison (test_parser.TestParser.test_unary_and_comparison) ... ok
test_undeclared_use (test_parser.TestParser.test_undeclared_use) ... ok
test_critical_edge_split_exists (test_pipeline.TestEndToEnd.test_critical_edge_split_exists) ... ok
test_division_by_zero_reports_source_line (test_pipeline.TestEndToEnd.test_division_by_zero_reports_source_line) ... ok
test_example_files (test_pipeline.TestEndToEnd.test_example_files) ... ok
test_exec_cfg_consistency (test_pipeline.TestEndToEnd.test_exec_cfg_consistency) ... ok
test_exec_no_memory_or_phi (test_pipeline.TestEndToEnd.test_exec_no_memory_or_phi) ... ok
test_infinite_loop_guard (test_pipeline.TestEndToEnd.test_infinite_loop_guard) ... ok
test_no_phis_after_elimination (test_pipeline.TestEndToEnd.test_no_phis_after_elimination) ... ok
test_programs (test_pipeline.TestEndToEnd.test_programs) ... ok
test_truncation_semantics (test_pipeline.TestEndToEnd.test_truncation_semantics) ... ok
test_unreachable_not_executed (test_pipeline.TestEndToEnd.test_unreachable_not_executed) ... ok
test_bad_source_400 (test_service.TestService.test_bad_source_400) ... ok
test_build_ir (test_service.TestService.test_build_ir) ... ok
test_eliminate (test_service.TestService.test_eliminate) ... ok
test_health (test_service.TestService.test_health) ... ok
test_interpret_flavors (test_service.TestService.test_interpret_flavors) ... ok
test_parse (test_service.TestService.test_parse) ... ok
test_pipeline_equivalence (test_service.TestService.test_pipeline_equivalence) ... ok
test_pipeline_span_preserved (test_service.TestService.test_pipeline_span_preserved) ... ok
test_routes_complete (test_service.TestService.test_routes_complete) ... ok
test_ssa_route (test_service.TestService.test_ssa_route) ... ok
test_unknown_route (test_service.TestService.test_unknown_route) ... ok
test_diamond_phi (test_ssa.TestSSAConstruction.test_diamond_phi) ... ok
test_loop_carried_phis (test_ssa.TestSSAConstruction.test_loop_carried_phis) ... ok
test_no_memory_instructions (test_ssa.TestSSAConstruction.test_no_memory_instructions) ... ok
test_short_circuit_results (test_ssa.TestSSAConstruction.test_short_circuit_results) ... ok
test_single_definition (test_ssa.TestSSAConstruction.test_single_definition) ... ok
test_unreachable_blocks (test_ssa.TestSSAConstruction.test_unreachable_blocks) ... ok
test_validator_detects_bad_phi_incoming (test_ssa.TestSSAConstruction.test_validator_detects_bad_phi_incoming) ... ok
test_validator_detects_duplicate (test_ssa.TestSSAConstruction.test_validator_detects_duplicate) ... ok
test_validator_detects_use_before_def (test_ssa.TestSSAConstruction.test_validator_detects_use_before_def) ... ok

----------------------------------------------------------------------
Ran 58 tests in 4.240s

OK

############ 2) 样例程序 raw / ssa / exec 三形态对照 ############
--- diamond.toy args=[4]
SSA 校验通过：每个值单一定义，所有使用均被定义支配，φ 入边完整。
 raw: 返回 14，执行 15 步
 ssa: 返回 14，执行 12 步
exec: 返回 14，执行 13 步
（三种形态步数不同属正常：SSA 去除了 load/store）
三种形态返回值一致 ✓
--- diamond.toy args=[-3]
SSA 校验通过：每个值单一定义，所有使用均被定义支配，φ 入边完整。
 raw: 返回 17，执行 15 步
 ssa: 返回 17，执行 12 步
exec: 返回 17，执行 13 步
（三种形态步数不同属正常：SSA 去除了 load/store）
三种形态返回值一致 ✓
--- loop_sum.toy args=[10]
SSA 校验通过：每个值单一定义，所有使用均被定义支配，φ 入边完整。
 raw: 返回 55，执行 146 步
 ssa: 返回 55，执行 93 步
exec: 返回 55，执行 115 步
（三种形态步数不同属正常：SSA 去除了 load/store）
三种形态返回值一致 ✓
--- loop_sum.toy args=[100]
SSA 校验通过：每个值单一定义，所有使用均被定义支配，φ 入边完整。
 raw: 返回 5050，执行 1316 步
 ssa: 返回 5050，执行 813 步
exec: 返回 5050，执行 1015 步
（三种形态步数不同属正常：SSA 去除了 load/store）
三种形态返回值一致 ✓
--- unreachable.toy args=[]
SSA 校验通过：每个值单一定义，所有使用均被定义支配，φ 入边完整。
 raw: 返回 5，执行 7 步
 ssa: 返回 5，执行 6 步
exec: 返回 5，执行 6 步
（三种形态步数不同属正常：SSA 去除了 load/store）
三种形态返回值一致 ✓
--- unreachable2.toy args=[1]
SSA 校验通过：每个值单一定义，所有使用均被定义支配，φ 入边完整。
 raw: 返回 1，执行 14 步
 ssa: 返回 1，执行 12 步
exec: 返回 1，执行 12 步
（三种形态步数不同属正常：SSA 去除了 load/store）
三种形态返回值一致 ✓
--- unreachable2.toy args=[-5]
SSA 校验通过：每个值单一定义，所有使用均被定义支配，φ 入边完整。
 raw: 返回 4，执行 19 步
 ssa: 返回 4，执行 16 步
exec: 返回 4，执行 16 步
（三种形态步数不同属正常：SSA 去除了 load/store）
三种形态返回值一致 ✓
--- nested_branch.toy args=[7,7]
SSA 校验通过：每个值单一定义，所有使用均被定义支配，φ 入边完整。
 raw: 返回 101，执行 45 步
 ssa: 返回 101，执行 38 步
exec: 返回 101，执行 42 步
（三种形态步数不同属正常：SSA 去除了 load/store）
三种形态返回值一致 ✓
--- nested_branch.toy args=[-1,7]
SSA 校验通过：每个值单一定义，所有使用均被定义支配，φ 入边完整。
 raw: 返回 3，执行 34 步
 ssa: 返回 3，执行 29 步
exec: 返回 3，执行 33 步
（三种形态步数不同属正常：SSA 去除了 load/store）
三种形态返回值一致 ✓
--- swap_loop.toy args=[5]
SSA 校验通过：每个值单一定义，所有使用均被定义支配，φ 入边完整。
 raw: 返回 3，执行 97 步
 ssa: 返回 3，执行 63 步
exec: 返回 3，执行 87 步
（三种形态步数不同属正常：SSA 去除了 load/store）
三种形态返回值一致 ✓

############ 3) 手工 φ 交换环 ############
func swap(%n) // flavor: ssa
{
@entry:            ; preds: -
  %c1 = const 1
  %c2 = const 2
  %c0 = const 0
  %c1b = const 1
  %n = param 0
  jmp @head
@head:            ; preds: @entry, @latch
  %a1 = phi [@entry: %c1], [@latch: %b1]    ; slot %a
  %b1 = phi [@entry: %c2], [@latch: %a1]    ; slot %b
  %i1 = phi [@entry: %c0], [@latch: %i2]    ; slot %i
  %cond = lt %i1, %n
  br %cond, @body, @exit
@body:            ; preds: @head
  jmp @latch
@latch:            ; preds: @body
  %i2 = add %i1, %c1b
  jmp @head
@exit:            ; preds: @head
  %sum = add %a1, %b1
  ret %sum
}
func swap(%n) // flavor: exec
{
@entry:            ; preds: -
  %c1 = const 1
  %c2 = const 2
  %c0 = const 0
  %c1b = const 1
  %n = param 0
  %r0 = copy %c1
  %r1 = copy %c2
  %r2 = copy %c0
  jmp @head
@head:            ; preds: @entry, @latch
  %cond = lt %r2, %n
  br %cond, @body, @exit
@body:            ; preds: @head
  jmp @latch
@latch:            ; preds: @body
  %i2 = add %r2, %c1b
  %r2 = copy %i2
  %pcopy.t0 = copy %r1
  %r1 = copy %r0
  %r0 = copy %pcopy.t0
  jmp @head
@exit:            ; preds: @head
  %sum = add %r0, %r1
  ret %sum
}
n=0: a+b=3 ✓
n=1: a+b=3 ✓
n=2: a+b=3 ✓
n=3: a+b=3 ✓
n=4: a+b=3 ✓
n=5: a+b=3 ✓
φ 交换环端到端验证通过

全部通过
```

## 3. JSON 服务冒烟：bash examples/requests/curl_smoke.sh 8765
```
== /health ==
{
  "ok": true,
  "service": "ssa_tool",
  "routes": [
    "/health",
    "/parse",
    "/build_ir",
    "/ssa",
    "/eliminate",
    "/interpret",
    "/pipeline"
  ]
}
== /pipeline (diamond, a=4, 期望返回 14) ==
ok: True
执行: {'raw': {'value': 14, 'steps': 15}, 'ssa': {'value': 14, 'steps': 12}, 'exec': {'value': 14, 'steps': 13}, 'equivalent': True}
SSA违规: []
== /ssa (loop_sum, 看 φ) ==
ok: True 违规: []
  %s.1 = phi [@entry: %s.2], [@while.body.1: %s.3]    ; slot %s
  %i.1 = phi [@entry: %i.2], [@while.body.1: %i.3]    ; slot %i
== /interpret (nested, exec flavor) ==
{'ok': True, 'value': 2, 'steps': 40, 'trace': []}
== 错误请求 (未声明变量) ==
{
  "ok": false,
  "error": "ParseError",
  "message": "语法错误 (行 1, 列 14): 变量 'y' 未经声明"
}
冒烟完成
```
