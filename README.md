# 受限张量计算图的内存复用规划器与执行器

Python + FastAPI + NumPy 实现。所有输入均为本地合成夹具；无任何生产账号或真实
业务数据。规划器基于真实活跃区间与别名关系做缓冲复用，执行器按波障
（wave barrier）调度，动态形状容量不足时整图重规划，绝不越界沿用旧绑定。

## 目录结构

```
src/tensor_mem/
  tensor.py    张量类型：dtype/Shape/TensorMeta、严格输入校验、对齐字节数
  ops.py       算子契约：元数、shape 推断、别名规则、工作区、NumPy 内核
  graph.py     计算图：构建校验、拓扑排序、ASAP 波次划分（并发集合）
  liveness.py  活跃区间（闭波次区间）、别名并查集、冲突原因分类
  planner.py   最佳适配池分配、峰值/对齐/工作区/常驻核算、预算、重规划判定
  state.py     训练状态：参数/梯度/优化器槽、阶段状态机、梯度契约校验
  executor.py  池绑定执行、波障线程并发、动态重规划、输出句柄留存计费
  runlog.py    可重放 JSONL 日志（run id + 单调序号 + 中间状态 + 判断理由）
  fixtures.py  合成夹具与独立 NumPy 预言机（不经过任何被测代码）
  api.py       FastAPI 边界：会话/执行/留存释放，错误分类 -> HTTP 状态
tests/         pytest 套件（unit / integration / e2e 三类标记）
scripts/       run_demo.py 端到端核验；replay_log.py 日志重放检查
```

模块间错误契约：所有跨模块异常都是 `errors.PlannerError` 子类，带稳定
`category` 字段：`input_error` / `graph_validation_error` / `state_conflict`
/ `resource_exhausted` / `bind_capacity` / `computation_failure`。
HTTP 层分别映射为 422 / 400 / 409 / 507 / 500 / 422（响应体始终含 category）。

## 依赖版本（本机已验证）

| 组件 | 版本 |
|---|---|
| Python | 3.12.3 |
| numpy | 2.4.6 |
| fastapi | 0.141.1 |
| pydantic | 2.13.5 |
| uvicorn | 0.54.0 |
| httpx | 0.28.1（测试） |
| pytest | 9.1.1 |
| pytest-cov | 7.1.0（覆盖率，可选） |

安装（可选，也可直接用 `PYTHONPATH=src`）：

```bash
python3 -m pip install -e ".[server,test]"
```

## 本地验证命令

```bash
# 1) 完整测试套件（95 个测试，应全部通过）
PYTHONPATH=src python3 -m pytest tests/ -q

# 2) 覆盖率（当前总覆盖率 94%，全部模块 >= 90% 核心规划路径 >= 95%）
PYTHONPATH=src python3 -m pytest tests/ -q --cov=tensor_mem --cov-report=term-missing

# 3) 端到端场景核验（菱形/长寿命/形状突变/并发分支/别名/训练/错误分类）
#    退出码 0 且末尾打印 "ALL CHECKS PASSED"
python3 scripts/run_demo.py

# 4) 重放某次运行的日志（run id、每波驻留字节、槽位、赤字与失败原因）
python3 scripts/replay_log.py logs/demo-dynamic.jsonl

# 5) HTTP 服务手测
PYTHONPATH=src uvicorn tensor_mem.api:app --port 8912
curl -s http://127.0.0.1:8912/health
curl -s -X POST http://127.0.0.1:8912/synthetic \
  -H 'Content-Type: application/json' -d '{"case":"diamond","seed":11}'
```

### 预期判断方式（关键数字，64B 对齐）

- **菱形图** `x→relu→{add,mul}→add(y)`：波次
  `[relu] / [add_branch, mul_branch] / [join]`；连接输出 `y` 复用已死的
  中间量 `t` 的槽；峰值驻留 **256B**（波 1：x,t,a,m 四个 64B 槽）；
  不复用时总记录 **320B**。
- **长寿命输出**：被声明为图输出的 `early` 在整个执行期间常驻，后续波次
  不得覆盖；句柄释放前 `retained_bytes=128`（early+final），释放后归零；
  释放后再读句柄报 `state_conflict`。
- **形状突变**：静态按 (4,4)×(4,2) 规划（峰值 256B）；喂入 (32,4)×(4,16)
  时先产生 5 条容量赤字（required > capacity），整图重规划后峰值
  **4096B**（波 1 驻留 c=2048B 与留存 y=2048B）；同形再跑**不**重规划；
  缩小再跑沿用更大绑定也不重规划。rank/dtype 变化是 `input_error`，不会
  被当作动态形状。
- **并发分支**：N 个同波 matmul 的输出与工作区各占互斥槽（4 分支时
  5 feeds+4 输出+4 工作区 = 13×64B = **832B** 峰值）；线程池在波障后
  并发执行，重复 5 轮结果逐元素正确，证明在用缓冲未被共享。
- **别名**：reshape/transpose 是视图，并查集合并为同一别名类、同一槽、
  NumPy `shares_memory=True`；别名类不额外分配。
- **训练状态**：参数 W 作为外部存储注入（`val::W` 不进池分配表，
  `external=True`），但其对齐字节计入波 0 驻留；SGD 更新结果对照独立
  公式逐元素一致；非法阶段转换、梯度形状漂移、非有限梯度分别报
  state_conflict / state_conflict / computation_failure。
- **数值正确性**：所有场景输出对照 `fixtures.reference_outputs`
  （独立 NumPy 表达式，不调用被测核心）逐元素 `allclose`。
- **未通过/未运行**：如存在失败，pytest 以非零退出码列出；`scripts/run_demo.py`
  以退出码 1 并列出失败检查名。本交付中测试全部通过、全部已运行，无跳过。

## 算法假设（明确列出）

1. **波障执行模型**：同一波（ASAP 层）内节点无依赖，可在线程池中并发；
   波末有屏障。值的活跃区间为闭波次区间 `[生产波, 最后消费波]`，图输出
   为 `[生产波, +∞)`（客户端句柄释放前存活）。
2. **复用条件**：两条记录仅当闭区间严格不相交（`busy_until < birth_wave`）
   才可同槽；同波并发、重叠区间、留存输出、别名合并均在冲突分类器中
   显式给出原因（parallel_branch / concurrent_wave /
   overlapping_lifetime / retained_output_live）。
3. **别名**：reshape/transpose 输出是输入的 NumPy 视图，别名类生命周期与
   容量取成员并集与最大值；视图共享物理存储，不另开缓冲。
4. **对齐与工作区计峰**：每个张量按 64B（可配置）向上对齐；matmul 的打包
   面板工作区、reduce 的累加暂存按算子公式算出对齐字节，仅在所属波存活，
   与值缓冲同等参与放置、复用与峰值。
5. **动态形状**：静态图形状只作为 rank/dtype 契约。每次执行先把实际 feed
   形状经各算子推断传播，再对每条记录做容量复核；任何赤字都触发**整图
   重规划**（而非局部扩容），旧绑定绝不越界写。同形/缩小不重规划。
6. **峰值口径**：池在波障处按需 acquire/release，因此每波实际持有字节的
   高水位恰为规划的 `live_capacity`；外部（状态）存储另列
   `external_bytes`，两者之和 `resident_bytes` 为真实 RAM 峰值。需要说明：
   在严格波障模型下，同一波内所有当波存活值本来就必须同时驻留，所以复用
   主要降低的是**总场地预留（total_pool_bytes）与累计分配字节**，而非
   每波高水位（这是该模型的固有性质，测试与文档如实区分这两个量）。
7. **留存计费**：未释放输出句柄钉住其运行的持久槽；后续运行的预算校验按
   `本次规划峰值 + 此前未释放字节` 计费。
8. **外部状态**：训练参数由 `TrainingState` 持有并注入，不进可复用池；
   梯度形状/dtype 必须与参数契约一致，否则状态冲突。
9. **受限数值域**：dtype 仅 float32/float64/int32/int64；二元逐元素运算
   不做隐式广播；matmul 仅支持 rank-2；内核输出做非有限值检查。
10. **放置策略**：按出生波排序的最佳适配（最小足够容量、确定性 id 兜底），
    非全局最优染色，但产生无冲突放置并自校验（`Plan.assert_valid` 与
    `validate_assignment` 双重检查）。

## 日志（可重放）

每次执行写 JSONL（内存模式保留在 logger.events；HTTP 会话落盘
`logs/<sess>.jsonl`）。事件含 `run_id`、单调 `seq`、时间戳、kind、
判断 message 与关键 data：feed 具体形状、每波 pool/external/resident
字节、节点输入输出形状与槽位、容量赤字明细、重规划前后峰值、失败 category
及关键账面数字。`scripts/replay_log.py` 校验序号连续性、重建每波驻留
时间线并汇总失败原因。
