# 激活重计算调度（Activation Recompute Scheduler）

一个纯本地、确定性的后端项目：给定小型合成训练计算图，**规划激活检查点**并在
**反向传播中重算被丢弃的激活**，在显式内存预算下最小化额外计算量。技术栈
Python 3.12 + FastAPI + NumPy，无外部账号、无真实业务数据、无网络依赖。

---

## 1. 它解决什么问题

训练反向传播需要前向激活。全部保留最省算力但占内存；全部丢弃最省内存但要重算。
检查点（activation checkpointing / rematerialisation）在二者间取舍：

* **规划器**枚举/搜索"保留哪些节点"，在 `peak_memory <= budget` 的方案中最小化
  **额外重算量**，并把额外算力、峰值内存、保留激活、临时工作区都显式报出；
* **执行器**真实跑 NumPy 前向/反向，按计划丢弃与重放激活，产出损失与梯度；
* **数值核验**用一套**独立实现**的即时自动微分 oracle 与中心有限差分对照，
  参考答案不由被测核心生成。

---

## 2. 目录结构与模块边界

```
app/
  core/                  # 被测核心（各模块有明确数据/错误契约）
    tensor.py            # 张量类型、形状/dtype/有限性校验、元素计数
    ops.py               # 算子：形状推导、算力/工作区成本、前向、VJP
    graph.py             # 计算图：DAG 校验、拓扑序、共享子图引用计数
    rng.py               # 确定性随机流（counter / snapshot 两种重放策略）
    state.py             # 训练状态：参数、梯度、SGD 生命周期
    memory.py            # 离散事件内存/成本仿真 + 反向调度表（规划与执行共享）
    planner.py           # 检查点规划：小图穷举 / 大图贪心
    executor.py          # 真实前向 + 检查点反向重放执行
    numeric.py           # 数值比对、指纹工具
    errors.py            # 四类错误契约（见下）
  reference/             # 独立参考实现（只依赖 numpy + 纯数据夹具）
    seedspec.py          # 文档化的 dropout 播种规则（独立复现掩码）
    oracle.py            # 独立即时前向/反向自动微分
    finite_diff.py       # 中心有限差分数值梯度
    liveness.py          # 独立的峰值内存/重放成本仿真器
  fixtures/
    specs.py             # 中立纯数据夹具（图、输入、参数种子）
    graphs.py            # 把纯数据构造成核心对象
  api/schemas.py         # Pydantic 请求/响应模型（请求与响应分离）
  service/               # 编排、会话存储、JSONL 运行日志
  main.py                # create_app() 工厂与瘦路由
tests/                   # pytest：unit / integration / e2e
examples/                # 请求样例
```

错误四分类（可区分、各有稳定错误码，HTTP 映射见 `app/main.py`）：

| 类别 | category | HTTP | 典型码 |
|---|---|---|---|
| 输入错误 | `input_error` | 400 | `E_SHAPE_MISMATCH`, `E_GRAPH_CYCLE`, `E_TENSOR_NONFINITE` |
| 状态冲突 | `state_conflict` | 409 | `E_STATE_NO_PLAN`, `E_STATE_RUN_DOUBLE_APPLY` |
| 资源耗尽 | `resource_exhausted` | 507 | `E_BUDGET_INFEASIBLE` |
| 计算失败 | `computation_failure` | 500 | `E_COMP_NONFINITE`, `E_MEMORY_DIVERGENCE`, `E_REPLAY_DIVERGENCE` |

---

## 3. 内存与重算模型（关键工程契约）

成本单位统一为 **float64 元素个数**（夹具同 dtype，便于断言精确整数）。

被跟踪的缓冲：

* 常驻根：所有 `input`/`parameter`；
* 保留激活（检查点，从前向存活到反向）与重放激活（反向惰性物化）；
* 每个内部节点/参数一个梯度累加缓冲；
* **临时工作区**：算子私有 scratch（linear/dropout 为输出大小，reduce_sum
  为输入大小）+ 每个 VJP 输入的瞬时梯度贡献缓冲；
* snapshot RNG 策略下图含 dropout 时常驻一份 624 字 MT19937 状态快照。

重放与引用计数规则：

1. 前向中非检查点激活在其**最后一个前向消费者**之后立即释放；
2. 反向按逆拓扑序。微分节点 `w` 前，把其 VJP 需要的输入激活（seeds）备齐，
   缺失的 seed 通过重放祖先子 DAG（到最近的检查点/根边界）物化；
3. **单个重放波内**，中间结果按拓扑产生、在其波内最后读取后立即释放
   （只保留一个小前沿，因此检查点能真正压低线性链峰值）；
4. **跨波仍被读取的重放值用引用计数持有，且只物化一次**——共享子图备忘，
   每个激活恰好一次生产事件；
5. dropout 的 VJP 需要随机掩码：被重放的 dropout 在其重放波产出掩码并持有到
   自身 VJP；被保留的 dropout 在自身 VJP 步瞬时重抽（mask-only）。

两套独立实现（`app/core/memory.py` 与 `app/reference/liveness.py`）在**全部
检查点子集**上逐值一致；执行器真实跑完还会断言实测峰值/重算量与仿真一致
（否则抛 `E_MEMORY_DIVERGENCE`），规划不会"对着记账幻觉做规划"。

### RNG 重放与副作用

* `counter`（默认）：每个 dropout 节点 `i` 用
  `SeedSequence([master_seed, sha256(i)[:4]])` 的独立生成器；重放时重建生成器，
  第一次抽取即前向掩码，与中间抽了多少别的节点无关。
* `snapshot`：前向前捕获全局 `RandomState`；每个重放波恢复快照，并用 dummy
  抽取空转到目标 dropout 序号，使第 k 次抽取恒为前向第 k 次。
* `external` 算子带**非幂等外部副作用**，只在前向触发一次；重放只做数值计算
  （`emit=False`）。响应与日志都回副作用次数，测试断言恰好为 1。

---

## 4. 从干净目录复现

需要 Python 3.12（开发环境：3.12.3，Linux）。

```bash
# 1) （可选）虚拟环境
python3 -m venv .venv && source .venv/bin/activate

# 2) 安装锁定依赖
pip install -r requirements.txt

# 3) 跑全部测试 + 覆盖率
python3 -m pytest tests/ --cov=app --cov-report=term-missing

# 4) 启动服务
uvicorn app.main:app --host 127.0.0.1 --port 8000
# 日志默认写到 ./logs/runs.jsonl（可用 ACT_RECOMPUTE_LOG_DIR 改目录）
```

无 pip 也可直接跑测试（环境已具备 numpy/fastapi/pytest 时）：

```bash
python3 -m pytest tests/ -q
```

---

## 5. 请求样例（端到端）

内置夹具：`linear_chain`（线性小链，可穷举）、`branching`（分支 + dropout +
共享子图 + 外部副作用）、`tight_budget`（用于验证预算不可行）。

```bash
# 健康检查 / 夹具目录
curl -s localhost:8000/health
curl -s localhost:8000/fixtures

# 1) 建会话（固定种子 = 固定 dropout 夹具）
SID=$(curl -s -X POST localhost:8000/sessions \
  -H 'Content-Type: application/json' \
  -d '{"fixture":"branching","master_seed":1234}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["session_id"])')

# 2) 规划（不设预算：在可行域内最小化重算；也可 "memory_budget":120）
curl -s -X POST localhost:8000/sessions/$SID/plan \
  -H 'Content-Type: application/json' \
  -d '{"rng_strategy":"counter"}'

# 3) 执行训练步并做独立 oracle + 有限差分核验
curl -s -X POST localhost:8000/sessions/$SID/runs \
  -H 'Content-Type: application/json' \
  -d '{"master_seed":1234,"finite_difference":true}'

# 4) 应用一个 SGD 步（对同一 run 再次 apply 会得到 409 状态冲突）
curl -s -X POST localhost:8000/sessions/$SID/apply \
  -H 'Content-Type: application/json' -d '{"lr":0.1}'

# 5) 预算不可行 -> 507 resource_exhausted / E_BUDGET_INFEASIBLE
SID2=$(curl -s -X POST localhost:8000/sessions -H 'Content-Type: application/json' \
  -d '{"fixture":"tight_budget"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["session_id"])')
curl -s -X POST localhost:8000/sessions/$SID2/plan \
  -H 'Content-Type: application/json' -d '{"memory_budget":1}'
```

完整请求体见 [`examples/requests.md`](examples/requests.md)，也可用
`examples/run_demo.sh` 一键跑。

### 预期关键数值（counter 策略，无预算）

| 夹具 | 检查点 retained | 峰值内存(元素) | 额外重算(flops) | 说明 |
|---|---|---|---|---|
| linear_chain | h1,a1,h2,loss | 90 | 0 | 全保留最优；理论最小峰值 84（代价 48 flops） |
| branching | d1,shared,ext,loss | 114 | 16 | d1 保留但掩码仍需 mask-only 重放 |
| tight_budget | h,loss | 96 | 0 | 预算 1 不可行，最小可达峰值 96 |

branching 执行响应里 `verification.passed=true`、`emitted_side_effects=1`、
各参数梯度对独立 oracle 的最大绝对差 ≤ 1e-8，对有限差分 ≤ 1e-5。

---

## 6. 第三阶段的验证案例（均断言具体结果，而非"接口能调"）

* **梯度一致（固定 dropout + 分支图）**：对两种 RNG 策略、以及保留/重放
  dropout 的方案，参数与输入梯度都与独立 oracle 一致（≤1e-10），并与中心有限
  差分一致（≤1e-5）；
* **小图穷举对照峰值内存与重算成本**：linear_chain 的全部 2^4 个检查点子集，
  核心仿真器与**独立** `LivenessSim` 在峰值、前向/重算/反向算力、精确重放节点
  集合上逐值相同；并对全部子集真实执行，实测峰值与仿真一致、梯度与 oracle 一致；
* **预算不可行**：预算低于最小可达峰值时返回 507 / `E_BUDGET_INFEASIBLE`，
  错误上下文给出 `min_achievable_peak` 与正的 `shortfall`，而不是交出一个
  "删掉必要激活后执行报错"的坏方案；
* **额外计算量明确**：计划始终带 `recompute_flops`/`extra_compute_ratio`，
  Pareto 前沿测试断言内存↔算力单调权衡（如 84 峰值↔48 flops，90 峰值↔0）；
* **共享子图引用计数**：branching 中被两支路消费的 `shared`/`W3` 在全重放下
  仍只物化一次（重放列表无重复）；
* **RNG 重放 / 无重复副作用**：断言掩码被重放而非重采样、外部副作用仅一次、
  重放波中副作用被抑制。

参考答案（oracle、有限差分、独立 liveness、固定掩码）全部位于 `app/reference/`，
不 import 任何 `app.core` 算法模块。

---

## 7. 运行日志（可重放问题）

每次建会话/规划/执行/失败向 `logs/runs.jsonl` 追加一行 JSON，含：

* `run_id`（时间戳 + 随机后缀，可排序、唯一）与 `session_id`；
* 关键中间状态：retained 集合、仿真峰值 vs 实测峰值、重算 flops、重放波、
  梯度内容指纹（sha256）；
* `judgement`：判定理由；
* 失败时的 `error_category` / `error_code` / `context`（四类可区分）。

拿到一行失败记录即可用相同 `fixture`/`master_seed`/预算重建现场。

---

## 8. 主要测试命令

```bash
python3 -m pytest tests/ -q                 # 全部
python3 -m pytest tests/unit -q             # 单元
python3 -m pytest tests/integration -q      # 集成（穷举/梯度/预算）
python3 -m pytest tests/e2e -q              # HTTP 端到端
python3 -m pytest --cov=app --cov-report=term-missing   # 覆盖率
```
