# 运行记录（RUNLOG）

日期：2026-09-24
环境：Linux 6.8（x86_64），Python 3.12.3，NumPy 2.5.3；无其他第三方依赖。

本文件如实记录实际执行过的命令与结果，包括开发过程中发现并修复的问题。

## 1. 自动化测试

命令（在项目根目录）：

```bash
python3 -m unittest discover -t . -s tests -v
# 或
bash run_tests.sh
```

最终结果（2026-09-24 实跑输出）：

```
Ran 28 tests in 0.735s

OK
```

28 个测试全部通过，清单：

| 文件 | 测试 | 覆盖点 |
|---|---|---|
| tests/test_bruteforce.py | 3 | 200 个随机小图中 120 个有效图（20 个含源点可达负环被跳过，其余为 s==t 候选）穷举逐流量费用对照、不可达汇点 0 流、孤立汇点 + required_flow 返回 infeasible |
| tests/test_fixed_cases.py | 5 | 平行边（含负费用平行边）、无负环的负边网络、两个必须沿残量反向弧撤销先前流量的反向增广固定用例，均含穷举逐流量对照 |
| tests/test_api.py | 15 | JSON 成功响应、required_flow 恰好/超限/0、无边图、严格类型校验（bool/float/string 拒绝）、缺字段、超范围、负环检测、零容量边不构成环、畸形 JSON、错误响应结构、CLI 子进程（stdin 成功、畸形 JSON 退出码 2、文件参数） |
| tests/test_properties.py | 3 | 30 张随机图上最终势函数的残量弧约化费用非负 + 守恒校验；总费用 1.1×10¹⁶ 的 Python 大整数精度；40×40 网格（n=1600, m=4680）20 秒预算内完成 |

穷举对照规模（与测试同种子的独立复核脚本实跑）：

```
候选200: 有效图 120 个, 跳过(含可达负环) 20 个, 逐流量费用对照点 140 个, 全部一致
```

每个对照点同时比较：指定流量是否 `optimal`、流量值、最小费用；
solver 内部与 API 出口各执行一次容量约束（0≤f≤cap）与流守恒
（源净出/汇净入=总流量，其余点净值 0）整数断言。

## 2. 请求样例实跑

命令：

```bash
for f in examples/0*.json; do python3 -m mcf.cli "$f"; echo "[exit=$?]"; done
```

结果（关键值；完整 JSON 可复跑查看）：

| 样例 | 结果 status | flow | cost | 退出码 |
|---|---|---|---|---|
| 01_basic.json（基本，含负费用边） | optimal | 15 | 29 | 0 |
| 02_required_flow.json（指定流量 2，反向增广图） | optimal | 2 | −200 | 0 |
| 03_parallel_edges.json（三条平行边） | optimal | 7 | 1（=3·1+2·4+2·(−5)） | 0 |
| 04_unreachable_sink.json（汇点孤立） | optimal | 0 | 0，汇点势函数 null | 0 |
| 05_infeasible.json（要求 10，最大流 5） | **infeasible** | 5 | 15 | 0 |
| 06_negative_cycle.json（0→1 −2、1→0 +1） | error: negative_cycle | — | — | **2** |

畸形/非法输入抽查：

```text
'{bad'                                          -> error.invalid_json, exit 2
capacity: true（布尔冒充整数）                  -> error.invalid_request, exit 2
'{}'（缺字段）                                  -> error.invalid_request（列出缺字段），exit 2
```

## 3. 性能与规模（非正式基准，单次实跑）

| 场景 | 规模 | 墙钟时间 | 备注 |
|---|---|---|---|
| 随机分层图 | n=2000, m=10000（上限） | 3.64 s（含解释器启动，CLI 子进程） | flow=2, 3 次增广 |
| 单位容量稠密 DAG | n=500, m=5434 | 0.38 s（CLI 子进程） | flow=11, 12 次增广 |
| 2000 条单位容量平行 s-t 边（对抗：每路瓶颈 1） | n=4, m=2002 | 5.34 s（CLI 子进程） | flow=2000, 2001 次增广 |
| 40×40 双向网格 | n=1600, m=4680 | 0.049 s（进程内） | flow=2000, 3 次增广 |

SSP 增广次数最坏与总流量同阶（未做容量缩放）；上表对抗例为该限制的实测表现。

## 4. 大整数精度实跑

```python
# 10000 条平行边 cap=1e9, cost=1e6 => 总费用 1e19 > int64 上限 9.22e18
flow = 10000000000000
cost = 10000000000000000000   # 精确，通过容量/守恒校验
```

距离与势函数始终在 int64 安全域（|dist| ≤ (n−1)·10⁶ = 2×10⁹）；
总费用由 Python int 累计并与 `sum(flow·cost)` 直接核算值断言一致。

## 5. 开发中发现并修复的问题（如实记录）

以下两项在首跑测试时暴露，均已修复并通过回归；修复后全部 28 个测试通过。

1. **n<2 / 顶点越界时构造网络即 IndexError**
   现象：请求 `{"n":1,...}` 在 `FlowNetwork.__init__` 建邻接表时抛出
   `IndexError: list index out of range`，没有转成 JSON 错误响应。
   修复：`mcf/api.py::parse_request` 在构造网络前先校验
   n≥2、n≤上限、源汇编号与互异、边数上限及每条边的端点编号
   （commit 工作区当前版本）。回归测试：`test_out_of_range` 等。

2. **两个中规模/随机测试图自身含可达负费用环**（违反输入前提，非算法 bug）
   现象：`test_potential_invariant_on_random_graphs` 的随机图与
   `test_grid_40x50_runs_fast` 的 40×40 网格（东西向双向边费用
   −3 与 +2，2-环费用 −1）触发 `NegativeCycleError`。
   处置：随机图测试用独立 Bellman–Ford 参考实现跳过负环图
   （300 次尝试取 30 张有效图）；网格西向边费用由 +2 改为 +4，
   保证任意东西向 2-环费用和 ≥1 且无北向边，故无可达负环。
   算法对真实负环输入的拒绝行为保留，并由 `test_negative_cycle_detected`
   与 examples/06 覆盖。

另：测试发现方式必须从项目根以包导入
（`python3 -m unittest discover -t . -s tests`），否则测试模块内的
相对导入失败；已写入 README、run_tests.sh 与本文件。

## 6. 未通过项

最终状态：**无未通过项**（28/28 测试通过，样例、边界与压测结果如上）。
