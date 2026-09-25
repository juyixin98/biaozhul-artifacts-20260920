# 运行日志（如实记录）

环境：Ubuntu 6.8、Python 3.12.3、NumPy 2.5.3、pytest 9.1.1。无网络下载，全部数据为本地合成数据。

## 执行过的命令与结果

| 命令 | 结果 |
|---|---|
| `python3 -m pytest tests/ -q`（首次全量） | **4 failed, 57 passed**（见下“开发中遇到的失败”） |
| 修复后 `python3 -m pytest tests/ -q` | **63 passed** |
| 补充 CLI/生命周期测试后 | **72 passed** |
| `python3 -m pytest --cov=src/tensor_planner --cov-report=term-missing`（最终） | **72 passed，总覆盖率 95%**（executor/planner 100%、dag 96%、liveness 94%、api 91%、verifier 88%、cli 89%，均 ≥80%） |
| 4 个样例 `python -m tensor_planner.cli run ... -o ...` | 退出码均为 0，报告 `passed=true` |
| `python3 scripts/run_demo.py` | 4 场景全部 `passed=True`，最大绝对误差均为 `0.0` |

> 覆盖率在代码评审重构（拆分超长函数、删除死代码）后从 94% 升到 95%，
> 测试数不变（72），所有场景输出保持一致。

## 开发中遇到的失败（首轮 4 个，均已修复并复测通过）

1. `test_slice_request`：断言 arena=34，实际 28。
   **原因**：该请求里 `P` 形状是 `[4,2]`（8 元素），而单元测试夹具 `shared_view_dag`
   用的是 `[4,3]`（12 元素）。手工推演确认 28（X16+P8+A4）才是该请求的正确值，
   **修正测试期望值为 28**，规划逻辑无需改动。
2. `test_reuse_matches_naive[shared_view]`：断言两执行器峰值相等，实际 34≠36。
   **原因**：断言本身错误。共享视图下基线为每个切片复制独立缓冲（峰值 36），复用按
   物理存储计数（峰值 34），复用理应严格更低。改为：无视图场景断言峰值相等
   （`test_peak_equal_when_no_views`），有视图场景断言复用峰值严格更低
   （`test_shared_view_peak_strictly_lower`）。这一区分也写进了 README“内存口径”。
3. `test_nested_slice_rejected`：期望报“单级别名”，实际先报“完整划分”。
   **原因**：校验顺序问题——划分校验在嵌套切片校验之前。把“禁止切片的切片”移到
   划分校验之前，并加注释说明顺序依赖。
4. `test_handle_request_end_to_end`：期望错误信息不匹配。
   **原因**：`"inputs": "AB"` 先触发“需要恰好 2 个 inputs”。修正匹配信息并重命名
   测试为 `test_inputs_must_be_list_of_strings`。

期间另有两次自我引入、当轮即改回的笔误（未进入提交）：
- 重构 `_value_live_profile` 时写出重复条件 `if ... t < t < ...`；
- 拆分 `DAGBuilder.build` 时一度把“完整划分”校验移回“嵌套切片”检查之前，
  差点重新引入第 3 条失败；复跑 `test_nested_slice_rejected` 后发现并修正，
  最终让单级别名检查先于划分检查，并删除了重复的划分校验方法。

## 手工核对的关键放置（场景 03 共享视图）

```
P  offset=0  size=12 alive[0,4)
X  offset=12 size=16 alive[0,4)  members=[X, v, w]   # v→偏移0, w→偏移8
A  offset=28 size=6  alive[3,5)
B  offset=0  size=6  alive[4,5)                      # 复用 P 死后的槽位
arena=34, peak=34 @t=3
```

- 视图 `v/w` 无独立槽位，直接落在基底 `X` 槽内（C 布局偏移 0 / 8）；
- `B` 在 t=4 出生时 `P` 刚读完死亡，端点相接复用 offset=0；
- `check_no_overlap` 对所有时刻两两检查，无冲突。

## 未通过项 / 已知限制

- 无未通过测试；72/72 通过。
- 未安装 `ruff/black`（环境无），以 AST 脚本自查无未使用导入，风格遵循 PEP8。
- 能力限制（设计如此，非遗留缺陷）：仅 add / 二维 matmul / 单级 slice；
  切片须构成完整不重叠划分；每时刻至多一个 op；best-fit 不保证全局最优。
- NumPy 内核自身的临时输出缓冲不计入静态 arena 预算，README 已明确说明口径。
