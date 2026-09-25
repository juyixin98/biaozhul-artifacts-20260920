# 张量内存复用规划（Tensor Memory Reuse Planner）

纯后端、零外部模型/数据依赖的静态张量计算 DAG 内存规划服务。使用 **Python 3.10+ 与 NumPy**，以可复现的合成数据验证核心机制：在形状已知的加法（`add`）、矩阵乘法（`matmul`）和切片（`slice`）DAG 上，基于**最后使用点（last-use）**复用缓冲区，正确处理**切片视图别名**，并校验任意时刻活跃张量的存储窗口互不重叠、峰值满足预算。

没有前端；所有交互通过 JSON 请求（文件 / stdin / Python API）完成。

---

## 1. 核心机制

### 1.1 生死分析（Liveness）

时间轴是 DAG 的拓扑序（定义序）下标。根张量（输入 / `add` / `matmul` 结果）拥有一块连续扁平存储；其生命周期为闭区间 `[birth, last_use]`：

- `birth`：该张量被定义（算子写入完成）的时刻；
- `last_use`：它**最后一个消费者**执行的时刻；DAG 输出被钉在最终时刻。
- 两个缓冲区在 `[b1, l1]`、`[b2, l2]` 上冲突当且仅当 `b1 <= l2 且 b2 <= l1`（最后一个消费者读取时，自身输出正在写入，故端点相接算重叠）。

### 1.2 切片视图别名（Slice-view aliasing）

`slice` 节点不拥有存储，它是其源张量的视图（NumPy 基本索引风格、逐轴 `start:stop`、步长恒为 1）。沿切片链回溯得到**别名根（root）**，同一家族（root + 全部视图）共享一块窗口；家族的 `last_use` 取所有成员的最大最后使用点——因此"很久以后才被读取的视图"会阻止根缓冲被提前复用。多跳切片通过链式坐标平移换算出视图在根扁平窗口中的全局 `[min_offset, end_offset)`。

### 1.3 贪心 best-fit 复用

按拓扑序遍历：先回收 `last_use < 当前时刻` 的槽位，再从空闲槽位中选择**容量足够且最小**者复用（best-fit）；都放不下则向 arena 高地址追加。同时报告与放置策略无关的**理论峰值下界**（任意时刻活跃张量大小之和的最大值），arena 不会小于它。

### 1.4 安全校验

规划完成后 `_verify_plan` / `overlapping_live_pairs` 强制断言：

1. 任意生命周期重叠的两个缓冲，其扁平窗口 `[offset, offset+size)` 不相交；
2. 每个切片视图触及的扁平区间严格落在其根窗口内部；
3. 可选 `peak_budget_elements` 预算：仅报告，或 `enforce_budget=true` 时直接失败（退出码 2）。

### 1.5 执行器对照（验收手段）

- **`no_reuse`**：每个张量一块独立 NumPy 存储，切片结果 `.copy()` 物化，全程不释放——直观正确的基线（峰值 = 所有张量大小之和）。
- **`reuse`**：只分配**一块**大小为规划值的 arena，所有根张量写入规划偏移，切片是对 arena 的**真实视图**。任何重叠/生死错误都会表现为真实的数据损坏，而非被隐藏。
- 两个执行器喂入同一份种子合成数据，逐输出做 `allclose` 数值比对。

---

## 2. 目录结构

```
.
├── src/tmem/
│   ├── dag.py        # DAG 建模、请求校验、形状推导（广播 add / 批量 matmul / slice）
│   ├── planner.py    # last-use 生死分析、别名家族、best-fit 放置、重叠/峰值校验
│   ├── executor.py   # no_reuse 基线 vs 单 arena 复用执行器 + 数值比对
│   ├── service.py    # JSON 请求 -> 完整可序列化报告
│   └── cli.py        # 命令行入口（文件 / stdin）
├── examples/         # 4 个验收请求样例
├── tests/            # pytest：单元 + 服务/CLI + 随机 DAG 模糊测试（65 个）
├── reports/          # 运行时生成的完整 JSON 报告示例
├── RUNLOG.md         # 实际命令、输出与未通过项的如实记录
└── pyproject.toml
```

## 3. 安装与运行

仅需 NumPy：

```bash
pip install numpy
# 开发/测试可选：
pip install pytest pytest-cov
```

无需安装包，直接用 `PYTHONPATH=src` 运行：

```bash
# 完整 JSON 报告打印到终端
PYTHONPATH=src python -m tmem.cli examples/branch_merge.json

# 精简摘要
PYTHONPATH=src python -m tmem.cli examples/branch_merge.json --quiet

# 从 stdin 读取；超预算硬失败（退出码 2）
cat req.json | PYTHONPATH=src python -m tmem.cli - --enforce-budget

# 保存完整报告
PYTHONPATH=src python -m tmem.cli examples/shared_view.json \
    --save-report reports/shared_view_report.json
```

## 4. 请求格式

```json
{
  "inputs":  {"a": [2, 3], "w": [3, 4]},
  "ops": [
    {"name": "t1", "op": "matmul", "inputs": ["a", "w"]},
    {"name": "v",  "op": "slice",  "inputs": ["t1"],
     "starts": [0, 0], "stops": [2, 2]},
    {"name": "t2", "op": "add", "inputs": ["v", "b"]}
  ],
  "outputs": ["t2"],
  "seed": 143,
  "peak_budget_elements": 1000,
  "enforce_budget": false
}
```

- `inputs`：名字 → 形状（正整数维，至少 1 维）。
- `ops`：按拓扑序列出；名字即 SSA 值，只能引用此前已定义的张量。
  - `add`：恰好 2 个输入，遵循 NumPy 右对齐广播；
  - `matmul`：恰好 2 个输入，支持向量/矩阵/批量（≥2 维批次广播），收缩维必须相等；
  - `slice`：恰好 1 个输入，逐轴 `starts`/`stops`（半开区间，步长 1），维数须与源一致。
- `outputs`：非空；输出张量存活到执行结束。
- `seed`：合成数据种子（默认 143），保证可复现。
- `peak_budget_elements` / `enforce_budget`：峰值预算（float32 元素数）与是否硬执行。

### 资源与健壮性约束

本地服务对单个请求设置了上限，超限在**校验阶段**直接拒绝（退出码 2），不会尝试分配或执行：

- 单维最大 `1,000,000`，单张量最多 `100,000,000` 元素（float32 约 400 MiB）——这也拦截广播爆炸（如 `[1,100000] + [100000,1]` → 1e10）；
- 单个 DAG 最多 `10,000` 个算子；
- `seed` 必须是整数（非布尔）；非法请求、非法 JSON、不可读/不可写路径均返回退出码 2，不产生 Python 栈追踪。

## 5. 验收样例

| 样例 | 构造的场景 |
|------|-----------|
| `multi_consumer.json` | `t1` 同时被 `t2`(add) 与 `t3`(matmul) 消费，验证存活到最后消费者 |
| `shared_view.json` | 同一根 `X` 被两个视图共享、一个视图多消费者、还有链式视图（view 的 view） |
| `branch_merge.json` | 菱形分支汇合：`x` 扇出两路，切片后 `add` 汇合、独立 matmul 后再 `add` 汇合 |
| `peak_budget_chain.json` | 8 段单消费者 matmul 链；预算设为理论下界 192 并硬执行 |

实测峰值（float32 元素数）：

| 样例 | no_reuse | reuse | 下降 |
|------|---------:|------:|-----:|
| multi_consumer | 60 | 44 | 26.667% |
| shared_view | 93 | 65 | 30.108% |
| branch_merge | 118 | 64 | 45.763% |
| peak_budget_chain | 640 | 192 | 70.0% |

所有样例：活跃张量窗口互不重叠（safety=OK）、两执行器输出逐元素一致（numeric_match=OK）。

## 6. Python API

```python
from tmem import build_dag, plan_memory, execute
from tmem.service import handle_request

request = {...}
dag = build_dag(request)              # 校验 + 形状推导
plan = plan_memory(dag, peak_budget_elements=1000, enforce_budget=False)
plan.arena_elements                   # arena 大小（元素）
plan.buffer_for("t1").window          # (offset, offset+size)
plan.views                            # 切片视图元数据

run = execute(dag, mode="both")       # 两种执行器 + allclose 比对
run["comparison"]["match"]

report = handle_request(request)      # 完整 JSON 可序列化报告
```

## 7. 运行测试

```bash
PYTHONPATH=src python -m pytest tests/ --cov=src/tmem --cov-report=term-missing
```

含 200 个内置随机 DAG 模糊用例（随机形状、广播、多消费者、共享/链式视图、分支汇合）；
开发期另离线运行 1000 个随机 DAG 做安全+数值验证、3000 个随机图对 O(n log n) 重叠校验器与 O(n²) 暴力法做等价对照，均通过。

## 8. 范围与限制（刻意保持小而清晰）

- 仅支持形状静态已知的 `add` / `matmul` / `slice`（步长 1、逐轴区间）；没有动态形状、控制流、卷积或其他算子。
- 放置策略是贪心 best-fit（经典区间图着色的实用近似），不保证全局最优；报告同时给出理论峰值下界供对照。
- 数据类型固定 float32；不涉及持久化、网络服务或前端。
