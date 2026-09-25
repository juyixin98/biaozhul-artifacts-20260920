# 运行记录（RUN_LOG）

本文件如实记录开发过程中的实际命令、结果，以及测试首轮发现的问题与修复。
所有数据来自本机真实执行（非编造）。时间数字在不同机器上会有差异。

- 日期：2026-09-24
- 环境：Linux 6.8.0，Python 3.12.3，NumPy 2.5.3
- 依赖安装：`pip install -r requirements.txt`（仅 `numpy>=1.24`）

## 1. 自动化测试

命令：

```bash
python3 run_tests.py
```

最终结果（退出码 0）：

```
通过 15 个，失败 0 个
全部通过。
```

15 个测试函数：

| 测试 | 内容与实测输出 |
|---|---|
| `test_enumeration.test_exhaustive_tiny` | n≤5、容量 0..6、含 0 重量与负价值，**2520 个实例**全部与 2^n 枚举真值一致 |
| `test_enumeration.test_random_fuzz_medium` | 120 个 n≤16 随机实例与枚举真值一致 |
| `test_enumeration.test_zero_weight_negative_and_equal_density` | 零重量必取/必舍、容量 0、全负价值空解、同密度双最优解 |
| `test_enumeration.test_classic_cases` | 教科书实例、超重剔除、空实例 |
| `test_bounds.test_bound_is_valid_random` | 300 个随机实例，独立 `Fraction` LP 实现核对 `obj ≤ UB ≤ LP` |
| `test_bounds.test_fractional_bound_unit` | 分数上界手算核对（7.4 = 37/5、容量 0、容量充足） |
| `test_bounds.test_gap_nonnegative_and_never_false_optimal` | gap 恒非负，optimal 时无间隙 |
| `test_timeout.test_timeout_returns_valid_incumbent_and_bound` | 500 件强相关、10ms（seed=3）：`status=timeout, obj=144019, UB=144418, gap=0.2763%, nodes=256` |
| `test_timeout.test_tiny_timeout_is_deterministic_timeout` | 1μs 时限：`status=timeout, obj=64593, UB=64620, gap=0.0418%` |
| `test_timeout.test_more_time_closes_gap_and_matches` | 超时解 → 放宽时限后最优；最优值不超过超时上界 |
| `test_timeout.test_timeout_solution_always_feasible_many_seeds` | 10 个实例超时解全部可行自洽（7 个报告 timeout） |
| `test_api.test_validation_errors` | 缺字段/类型错/负数/超长/timeout 越界等全部结构化拒绝 |
| `test_api.test_boundary_sizes_accepted` | n=0 与 n=2000 边界规模均 `optimal` |
| `test_api.test_cli_file_and_stdin_and_bad_json` | stdin、文件、非法 JSON（退出码 2）、校验失败（退出码 2） |
| `test_api.test_cli_example_file` | examples/ 下全部合法样例真实跑通 |

## 2. CLI 样例实测

```
$ python3 -m knapsack.cli -f examples/request_basic.json                       ; exit=0
  status=optimal objective=17 selected=[0,1] total_weight=10 UB=17 gap=0

$ python3 -m knapsack.cli -f examples/request_zero_weight.json                 ; exit=0
  status=optimal objective=19 selected=[0,2,3] total_weight=7
  forced_zero_weight=[0] excluded_overweight=[4]

$ python3 -m knapsack.cli -f examples/request_timeout.json                     ; exit=0
  status=timeout objective=4312 UB=4316 gap=0.0927%
  nodes=256 elapsed≈0.0014s total_weight=4009 ≤ capacity=4012

$ python3 -m knapsack.cli -f examples/request_invalid_negative_capacity.json   ; exit=2
  {"ok": false, "error": {"code": "validation_error",
   "message": "'capacity' 必须 >= 0，实际为 -5", "field": "capacity"}}

$ python3 -m knapsack.cli -f examples/request_invalid_length_mismatch.json     ; exit=2
  {"ok": false, "error": {"code": "validation_error",
   "message": "'weights' 与 'values' 长度必须相等：2 != 1", "field": "values"}}
```

超时样例（120 件强相关、1ms）返回的解可独立复核：选中子集总重 4009 ≤ 4012，
价值 4312；上界 4316 有效，间隙 0.0927%，状态如实为 `timeout`（没有冒充最优）。

## 3. 规模/性能实测（10s 时限）

随机无关联与“尖峰”相关实例均可闭合到最优：

```
uncorrelated  n= 100  optimal  obj=  4257   nodes=  228   0.002s
uncorrelated  n= 500  optimal  obj= 20156   nodes= 1643   0.020s
uncorrelated  n=2000  optimal  obj= 82861   nodes= 3999   0.172s
spike         n= 100  optimal  obj=  2621   nodes= 1788   0.002s
spike         n= 500  optimal  obj= 66059   nodes= 7073   0.016s
spike         n=2000  optimal  obj=1055188  nodes=12335   0.198s
```

经典难例类（强相关 `v = w + k`）在大规模/短时限下**不闭合**，按承诺返回
可行解 + 有效上界 + `timeout`：

```
strong n= 200 k=100 t=0.05s -> timeout  obj= 68604 UB= 68663 gap=0.0859% nodes= 73984
strong n= 500 k= 50 t=0.05s -> timeout  obj=146203 UB=146314 gap=0.0759% nodes= 50176
strong n=1000 k= 20 t=0.2s  -> timeout  obj=268728 UB=269190 gap=0.1716% nodes=215552
```

同一实例两次求解 `selected` 完全一致（确定性：通过）。

## 4. 首轮测试发现的问题与修复（如实记录）

首轮 `python3 run_tests.py` 结果为 **12 通过 / 3 失败**，逐项定位：

1. **测试参考实现缺陷（非求解器 bug）**：独立 LP 核对实现把负价值物品也按
   分数装入，导致 LP 上界偏小、误判求解器上界无效。修复：负价值物品在 LP
   最优解中必为 0，跳过。修复后 `obj ≤ UB ≤ LP` 在 300 个实例上成立。
2. **测试期望值写错**：CLI 用例 `(w,v)=(6,10),(4,7),(3,6)` 容量 10 的最优是
   10+7=**17**（测试误写成 13）。按真值改正。
3. **测试误用**：校验用例把一个合法请求当成错误请求断言。删除该误断言。

修复测试后重跑全绿；随后在代码评审中又发现并修复了**求解器自身的一个真实
语义缺陷**：

4. **根节点超时被错误升级为 optimal（重要）**：超时检查在节点出栈时进行。
   若在根节点处即超时（例如 1μs 时限），此时两个子节点还没入栈、栈为空，
   旧逻辑取“栈中开放节点上界最大值”得到空集，从而错误地认为搜索闭合、
   报告 `optimal`。修复：把**刚被中断的节点本身**（其子树尚未探索，但上界
   在它入栈时已算好）计入未探索空间的上界。新增回归测试
   `test_tiny_timeout_is_deterministic_timeout`，修复前该例错误输出
   `status=optimal`，修复后稳定输出
   `status=timeout, obj=64593, UB=64620, gap=0.0418%`。

此外在实现过程中还纠正过两处内部记账问题（压栈子节点的上界漏加路径已累积
价值、初始解重量字段冗余），均在首轮运行前的自查中修复。

## 5. 未通过项 / 已知限制

- 最终测试套件：**无未通过项**（15/15）。
- 强相关难例在给定短时限内不闭合属**预期行为**而非失败：系统按规格返回
  `timeout`、可行解与有效上界，不冒充最优；放宽时限后可闭合（见第 3 节）。
- 超时为挂钟时间、节点粒度检查（每 256 节点 + 根节点），实际耗时可能略超
  `timeout_seconds`（毫秒级），样例 0.001s 时限实测约 0.0014s。
