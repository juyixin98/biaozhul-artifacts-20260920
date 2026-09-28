# HVP Service — 受限可微表达式的 Hessian 向量积服务

纯后端服务（Python 3.12 + FastAPI + NumPy）。对受限的标量表达式，提供
函数值、梯度和 **Hessian–向量积（HVP）**，**全程不显式构造完整 Hessian**。

所有数据均为本地合成夹具，无外部账号、无真实业务依赖。

---

## 1. 它做什么

给定

- 输入张量布局（每个变量有名字和形状，标量用 `"shape": []`）；
- 一个由受限算子组成的标量表达式；
- 一个求值点 `x` 和方向向量 `v`（形状绑定到输入布局）；

服务返回 `f(x)`、`∇f(x)` 和 `H f(x) · v`，并可对其做**独立的数值验证**
（50 位高精度 `mpmath` 解释器 + 显式稠密 Hessian，以及对函数值的中心差分）。

支持算子：

| 类别 | 算子 |
|---|---|
| 一元 | `neg sin cos exp log sqrt abs relu sign` |
| 二元 | `add sub mul div max min` |
| 伪算子 | `get`（取张量分量）、`scalar`（注入常量） |

`abs / relu / sign / max / min` 为分段光滑算子，非光滑点会被**精确识别**
（`x == 0`、`x == y`），按配置**拒绝**或采用**指定次导数**。

---

## 2. 核心算法：forward-over-reverse

1. **前向**：在点 `x` 上求值原语标量图（同时做定义域/溢出检查）。
2. **反向（构造而非数值传播）**：反向模式把梯度构造成**一张新的符号计算图**
   - 每个原语节点有一个镜像值表达式 `val:<id>`（叶子仍是叶子，所以梯度图是
     `x` 的真函数）；
   - 伴随量按逆拓扑累加：一个节点有 *k* 条消费边就收到 *k* 条 `mul` 贡献、
     用 `add` 逐条累加——**共享子图既不漏加也不重复加**；
   - 所有梯度分量是同一张图的多个根，公共子表达式只存一次、只算一次。
3. **HVP**：以 `xdot = v` 为种子，对梯度图做一次**前向模式（切向/对偶数）**
   传播，根切向即 `H f(x) v`。代价是一次反向构树 + 两次图扫描，与 Hessian
   维度无关，任何地方都没有 n×n 矩阵。零方向传播出**精确的零**（非差分近似）。

---

## 3. 模块边界（工程结构）

```
hvpsvc/
  tensor.py       张量类型与输入布局：形状校验、扁平/嵌套映射、向量形状绑定
  graph.py        计算图：节点/表达式图、预算、非光滑配置、JSON 规格解析
  ops.py          算子目录：前向规则、kink 判定、次导数选择
  autodiff.py     前向/反向组合、符号梯度图、切向传播（HVP）
  state.py        训练/求值状态：注册、求值点版本管理、梯度图缓存、状态冲突
  validation.py   独立数值验证：mpmath 高精度解释器+显式 Hessian、中心差分
  errors.py       错误分类体系（见下）
  runs.py         运行日志：run_id、关键中间状态、JSONL 落盘与重放
  models.py       HTTP 请求/响应契约（Pydantic）
  service.py      编排层：状态 + AD 核心 + 验证 + 日志
  main.py         FastAPI 应用、错误分类→HTTP 状态码映射
tests/            独立测试（解析常量 / mpmath / 中心差分三类参考，互不同源）
examples/         核心直调示例、HTTP 示例与请求载荷
```

模块间通过 `errors.py` 的分类异常与显式数据契约通信。

### 错误分类（可区分、可诊断）

| category | 含义 | HTTP |
|---|---|---|
| `input_error` | 规格非法、形状/布局不符、未知算子 | 422 |
| `state_conflict` | 未设求值点、未知 state、版本过期 | 409 |
| `resource_exhausted` | 节点数/求值次数/墙钟预算超限 | 429 |
| `compute_failure` | 定义域错误（如 `log(x≤0)`）、非有限值/溢出 | 422 |
| `nonsmooth_point` | 命中非光滑点且策略为拒绝 | 409 |

每个错误带结构化 `detail`（节点、算子、输入、所处阶段、预算用量等）。

---

## 4. HTTP 接口

| 方法 路径 | 说明 |
|---|---|
| `POST /functions` | 注册变量布局 + 表达式（可带 `nonsmooth`、`budget`） |
| `POST /functions/{id}/point` | 设置求值点；返回新版本号与函数值；支持 `expected_version` 乐观并发 |
| `GET  /functions/{id}/value` | 当前点函数值 |
| `GET  /functions/{id}/gradient` | 反向模式梯度（按变量布局返回） |
| `POST /functions/{id}/hvp` | Hessian–向量积，请求体 `{"vector": {...}}` |
| `POST /functions/{id}/verify` | 独立数值验证（见下） |
| `GET  /runs/{run_id}` | 按 run_id 重放一次运行（含中间状态与判定理由） |
| `DELETE /functions/{id}` | 删除状态 |
| `GET  /healthz` | 存活检查 + 算子目录 |

### 表达式规格

```json
{
  "variables": [{"name": "x", "shape": [3]}, {"name": "y", "shape": []}],
  "expression": {
    "nodes": [
      {"id": "x0", "op": "get", "args": ["x"], "index": 0},
      {"id": "k",  "op": "scalar", "value": 2.0},
      {"id": "x0sq", "op": "mul", "args": ["x0", "x0"]},
      {"id": "f", "op": "add", "args": ["x0sq", "k"]}
    ],
    "output": "f"
  },
  "nonsmooth": {"policy": "reject", "subgradient": 0.0},
  "budget": {"max_nodes": 200000, "max_evals": 2000000}
}
```

- 标量变量可直接在 `args` 中按名引用；张量变量必须先用 `get` 取分量。
- `nonsmooth.policy`：`reject`（默认）或 `subgradient`。后者用
  `subgradient ∈ [0,1]` 指定次导：对 `relu/max/min` 直接作为凸次梯度权重；
  对 `abs/sign` 映射到 `[-1,1]`（即 `2s−1`）。

### 验证端点做了什么

`POST /functions/{id}/verify`（仅用于小维度，高精度参考限制 ≤64 个输入分量）：

1. 服务梯度 vs mpmath 50 位高精度梯度；
2. 服务梯度 vs 仅基于函数值的中心差分；
3. HVP vs 高精度**显式稠密 Hessian** 的 `H·v`（用户向量 + 两条固定种子的
   确定性随机方向，便于重放）；
4. 零方向返回精确零；
5. 基向量探测 Hessian 对称性（n≤16）。

每个检查都给出 `passed / reason / max_abs_error / max_rel_error /
expected / actual`，失败类别明确。命中非光滑点时，光滑性检查标记为
`skipped` 并说明原因，只保留精确零方向检查。

### 运行日志

每次操作生成 `run-<hex>`，追加写入 JSONL（默认 `logs/hvp_runs.jsonl`，
可用环境变量 `HVP_LOG_PATH` 覆盖）。成功记录函数值、梯度/HVP 范数、
访问的切向节点数、预算用量、向量范数等关键中间状态；失败记录错误分类与
`detail`。错误响应体也带 `run_id`，可用 `GET /runs/{run_id}` 直接重放。

---

## 5. 快速开始

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

# 直接使用核心库（无 HTTP）
python examples/core_example.py

# 启动服务
uvicorn hvpsvc.main:app --port 8000
# 另一个终端
python examples/http_example.py

# 测试与覆盖率
pytest
pytest --cov=hvpsvc --cov-report=term-missing
```

### curl 片段

```bash
curl -s -X POST localhost:8000/functions -H 'Content-Type: application/json' \
  --data @examples/quadratic.json
curl -s -X POST localhost:8000/functions/<id>/point \
  -H 'Content-Type: application/json' \
  -d '{"point": {"x": [0.3, -0.5, 0.8]}}'
curl -s -X POST localhost:8000/functions/<id>/hvp \
  -H 'Content-Type: application/json' \
  -d '{"vector": {"x": [1, -2, 3]}}'
```

---

## 6. 测试如何保证正确性（参考答案不来自被测核心）

- `tests/fixtures.py` 中是**手工推导**的解析答案（例如二次型的精确 Hessian
  常量、共享子图 `y²+y` 的解析 Hessian），与实现无关。
- `hvpsvc/validation.py` 是一份**独立重写的 mpmath 解释器**，不导入任何
  AD 核心代码；它在 50 位精度下用数值微分构造**显式稠密 Hessian**。
- 中心差分只调用图的**前向函数值**，不经反向/HVP 逻辑。
- 测试断言**具体数值与失败类别**（状态码、`category`、`detail`、run 日志
  内容），而非“接口能调通”。另含共享子图贡献计数（3 条消费边→3 条伴随
  贡献、镜像值节点唯一）、零方向精确为零、参数共享跨变量布局等结构断言。

最近一次本地验证：`54 passed`，行覆盖率约 89%（见 `pytest --cov`）。

---

## 7. 已知限制

- 表达式受限于上述算子目录；无控制流、无广播/逐元素张量级算子（张量按
  分量显式索引）。
- 非光滑点采用“指定次导数”约定；这是广义 Hessian 的一种选择，不代表
  Clarke 广义 Hessian 的全部集合，文档与验证结果中均显式标注。
- 高精度独立验证是 O(n²)，刻意限制在 ≤64 个输入分量，仅供核验。
- 状态与日志为单机内存/本地文件，未做持久化数据库、鉴权与水平扩展
  （按需求定位为本地合成服务）。
- 数值上仍受 float64 上溢约束；溢出与定义域错误会在具体节点/阶段被
  诊断为 `compute_failure`，不会静默产生 NaN。
