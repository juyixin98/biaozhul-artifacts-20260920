# 激活重计算调度（Activation Recomputation Scheduler）

一个自包含的后端服务：对**线性与带分支**的前向计算图做激活检查点（activation
checkpointing）规划，并在反向传播中通过**重放 RNG 状态**完成激活重计算。
全部数据为本地合成夹具，内存以 **float64 元素数**记账——不依赖真实 GPU、
分配器或任何生产账号/业务数据。

技术栈：Python 3.12 · FastAPI · NumPy · pytest。

---

## 1. 它解决什么问题

训练步的前向激活若全部保留到反向，峰值内存随深度线性增长。激活检查点只保留
少量"边界激活"，反向时从边界与保存的 RNG 状态**重算**被丢弃的内部激活，用
额外算力换内存。本项目把这件事做成可规划、可执行、可对账、可复现的工程：

1. **规划**：在拓扑序上把节点切成块（block）；块边界激活保留，检查点块在
   反向时从边界 + RNG 快照整块重放。小图上**穷举全部合法切分**，在内存预算内
   选择额外重算量最小的方案。
2. **执行**：真实前向 / 反向，块边界保存 RNG 快照，重放时恢复快照逐位复现
   dropout 掩码；外部副作用（随机掩码发放）前向只发一次，重放只校验、绝不重复。
3. **对账**：静态预测峰值、独立区间模型、引擎运行时高水位三方一致；梯度同时
   与"不检查点基线"和"独立有限差分黄金参考"核对。

### 内存模型包含哪些（对应需求"保留激活和临时工作区"）

峰值 = 图输入 + 参数 + 保留的边界/末块激活 + 为反向保留的 saved 缓冲
（ReLU 掩码、dropout 缩放掩码）+ 上游与中间梯度 + 参数梯度 + RNG 快照
+ **算子前向/反向临时工作区**（`workspace`）。每一类都有明确的存活区间。

---

## 2. 模块与工程边界

| 模块 | 职责 | 关键契约 |
|---|---|---|
| `errors.py` | 五类错误与稳定 `category` | input_error / invalid_plan / state_conflict / resource_exhausted / compute_failure |
| `tensor.py` | `Tensor` 与记账工作区 `Arena` | 引用计数、工作区、运行时预算硬拦截、泄漏即 state_conflict |
| `ops.py` | 算子原语库 | 形状推断、saved/workspace 静态大小、前/反向；随机性只经 `OpEnv` |
| `graph.py` | 计算图构建与校验 | 拓扑序、DAG、形状、共享子图（`consumers`）、死节点拒绝 |
| `state.py` | 训练状态 | 合成夹具、参数、`rng_snapshot/restore_rng`（按块号去重） |
| `memory.py` | 静态模拟器 | 事件流水账：峰值、接缝驻留分项、重算 FLOP |
| `planner.py` | 方案与规划 | 方案合法性（跨块边必须源自边界）、穷举、预算选择 |
| `engine.py` | 检查点执行引擎 | RNG 重放、引用计数、预算拦截、三方峰值对账 |
| `verification.py` | **独立**数值参考 | 独立前向 + 逐标量中心差分（不导入 engine/planner） |
| `journal.py` | 运行日志 | 可重放 run_id、关键中间状态、判断理由（JSONL） |
| `schemas.py` / `api.py` / `service.py` | HTTP 契约与编排 | 统一错误信封，错误类别 → HTTP 状态码 |

模块间不互相塞实现：planner 只依赖图的静态属性；engine 依赖 plan + state；
verification 只依赖图声明与 numpy。

### 错误类别 → HTTP 状态码

| category | HTTP | 触发示例 |
|---|---|---|
| `input_error` | 422 | 未知算子、形状不符、引用未定义节点、死节点 |
| `invalid_plan` | 422 | 跨块边不源自边界、空尾块 |
| `state_conflict` | 409 | 重复前向、RNG 快照重复恢复、张量泄漏/双重释放 |
| `resource_exhausted` | 507 | 规划期无可行方案（`phase="planning"`）/ 运行时超预算（`phase="runtime"`） |
| `compute_failure` | 500 | NaN/Inf、重放掩码不一致、峰值对账不符、梯度校验失败 |

---

## 3. 从干净目录复现

### 3.1 配置与依赖

```bash
cd b
python3 -m venv .venv && source .venv/bin/activate    # 可选
pip install -r requirements.txt
```

实测版本（见 `requirements.txt`）：

```
numpy==2.4.6  fastapi==0.141.1  uvicorn==0.54.0  pydantic==2.13.5
pytest==9.1.1  httpx==0.28.1  pytest-cov==7.1.0
```

所有张量为 float64（每元素 8 字节）；内存预算/峰值均以元素数给出，
乘以 8 即字节数。

### 3.2 运行测试（含覆盖率）

```bash
python3 -m pytest tests/ -q
python3 -m pytest tests/ --cov=recomp_scheduler --cov-report=term-missing
```

### 3.3 启动服务并发请求

```bash
uvicorn recomp_scheduler.api:app --host 127.0.0.1 --port 8000
```

```bash
# 线性链 + dropout，宽松预算（会选零重算基线），并做独立有限差分
curl -s 127.0.0.1:8000/api/v1/plans/run \
  -H 'Content-Type: application/json' \
  --data @examples/run_chain.json | python3 -m json.tool

# 跨块共享子图的分支图，紧预算（会选多块检查点，付出非零重算）
curl -s 127.0.0.1:8000/api/v1/plans/run \
  -H 'Content-Type: application/json' \
  --data @examples/run_branch.json | python3 -m json.tool

# 预算不可行 -> 507 resource_exhausted
curl -s -i 127.0.0.1:8000/api/v1/plans/run \
  -H 'Content-Type: application/json' \
  --data @examples/run_infeasible.json

# 小图穷举全部合法方案的峰值/重算成本
curl -s 127.0.0.1:8000/api/v1/plans/exhaustive \
  -H 'Content-Type: application/json' \
  --data @examples/exhaustive_chain.json | python3 -m json.tool

# 按 run_id 取回可重放的日志记录
curl -s 127.0.0.1:8000/api/v1/runs/<run_id> | python3 -m json.tool
```

### 3.4 直接用 Python（不经 HTTP）

```python
from recomp_scheduler.service import plan_and_run
from recomp_scheduler.journal import Journal

summary = plan_and_run(
    raw_nodes,                          # 同 API 的 nodes 声明
    outputs=["l2"],
    budget_elements=8000,
    seed=99,
    run_finite_difference=True,
    journal=Journal("logs"),
)
print(summary.plan.boundary_positions, summary.result.runtime_peak)
```

---

## 4. 第三阶段验证案例（均为断言具体结果）

1. **固定 dropout 夹具 + 分支图的梯度一致**
   `tests/test_engine.py::test_gradients_identical_across_all_legal_plans`
   分支图 64 个合法方案的每个参数/输入梯度与前向输出，都与不检查点基线
   **逐元素相等**（`assert_array_equal`）。
2. **小图穷举检查点选择对照峰值与重算成本**
   `tests/test_planner.py` 冻结表（手算/独立模型交叉确认）：
   - 最小图基线峰值 **78**（手算：常量 27 + 反向 h1 时 ws8/pgrad16/gacc6 等）；
   - 线性链 16 个方案的 `(峰值, 重算FLOP)` 全部冻结；
   - 大激活长链基线峰值 **10804**，最优检查点 **6838**（−37%），代价 2400 重算 FLOP。
3. **预算不可行**
   规划期 `BudgetInfeasibleError(phase="planning")` 带 `min_achievable_peak`；
   运行时越界 `phase="runtime"` 带 `over_by`；两者可区分，且分别经 API 返回 507。
4. **三方内存对账** `tests/test_memory_crosscheck.py`
   独立区间覆盖模型（`tests/independent_model.py`，不导入 `memory.py`）
   ↔ 生产静态模拟器 ↔ 引擎 Arena 高水位，在 5 张图共 **316 个合法方案**上相等。
5. **独立有限差分黄金参考** `tests/test_verification.py`
   逐标量中心差分核对所有输入与参数梯度，容差 1e-6（实测 ~1e-10）；
   并包含"污染梯度必须被 FD 判失败"的反向健全性测试。

> 参考答案不由被测核心自身生成：内存用独立区间模型，梯度用独立前向 +
> 有限差分，运行时峰值是真实分配事实。

---

## 5. 运行日志（可重放）

每次规划/执行/错误都写入 `logs/runs-YYYYMMDD.jsonl`，字段包含：

- `run_id`：形如 `step-20260928T052219Z-0001-d9cf51fe`（错误记录为 `error-…`），
  可据此精确定位；
- 关键中间状态：图指纹、种子、方案边界与块内容、预测/运行时峰值、
  接缝驻留分项、梯度范数、RNG 快照账（taken/restored）、副作用计数；
- `reason`：人类可读判断理由（含失败类别与对比数字）。

用同一份图声明 + `seed` + `budget_elements` 即可复现同一结果（夹具与
dropout 掩码都由种子确定）。

---

## 6. 关键设计说明

- **方案合法性**：跨块边只能源自块边界节点，否则重放时所需激活无法恢复——
  这在规划期就被 `invalid_plan` 静态拦截，而不是"删掉必要激活后在反向报错"。
- **重放语义**：检查点块保留**边界节点的激活和它自己的 saved 缓冲**；重放只
  重建块内节点。边界节点若本身是 dropout，其掩码属于保留的 saved，不重放。
- **共享子图引用计数**：边界激活按"后续消费块数量" `retain`，每个消费块
  反向拆帧释放一份，边界所属块自身反传后才真正释放；多消费者梯度贡献在同一
  梯度槽累加。
- **额外计算量明确**：`recompute_flops` = 各检查点块内部节点前向 FLOP 之和，
  由独立公式在测试中复核；最后一块不重放。

## 7. 目录

```
b/
├── recomp_scheduler/      # 包源码（13 个模块）
├── tests/                 # pytest 套件 + 独立参考模型
├── examples/              # 请求样例
├── requirements.txt
├── pyproject.toml
└── README.md
```
