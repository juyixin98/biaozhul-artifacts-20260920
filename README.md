# HVP Service — 受限可微表达式的 Hessian 向量积服务

纯后端服务：对客户端声明的标量值表达式计算图，提供 **Hessian-vector product
(HVP)**、函数值与梯度，**从不显式构造完整 Hessian**。技术栈：Python 3.12 +
FastAPI + NumPy，无外部账号或真实业务数据依赖。

## 核心算法

HVP 通过**双反向模式**（reverse-over-reverse）计算：

```
H(x) · v  =  ∇( x ↦ ⟨∇f(x), v⟩ )
```

1. 对前向图做第一次反向传播，用**同一套算子**把梯度表达为新的子图；
2. 构造标量 `φ = ⟨grad f, v⟩`（`v` 作为常量注入）；
3. 对 `φ` 再做一次反向传播，得到 `H·v`。

内存与计算量随图规模增长，而非 `O(n²)`。共享子图通过**伴随累加**处理：
同一节点的多个消费者的伴随用显式 `add` 节点合并，前向只算一次、贡献恰好
求和一次（`tests/test_hvp.py` 中 `z = x0·x1 + sin(x0); f = z² + z` 用例针对
mpmath 高精度 Hessian 验证）。

### 非光滑点

`abs` / `relu` 在零点是不可微的。求值器按 `kink_atol` 检测拐点：

- `nonsmooth_policy="reject"`（默认）：拒绝并返回 `nonsmooth_point` 错误，
  携带节点 id、算子与拐点位置；
- `nonsmooth_policy="subgradient"` + `subgradient=g0 ∈ [-1,1]`：在拐点处取
  指定次导（`abs` 取 `g0`，`relu` 取 `g0`）。所选次导局部为常数，因此拐点
  处对 HVP 的曲率贡献为 0（见「限制」）。

### 数值与预算诊断

- 每个节点求值后检查有限性：溢出（如 `exp(800)`）或定义域错误（如
  `log(-1)`）→ `computation_failure`，携带节点 id、算子、非有限元素数；
- 图节点数、组合程序节点数、墙钟时间均有预算（服务器硬顶 + 请求级收紧），
  超限 → `resource_exhausted`，携带观测值与上限；
- 每次请求有 `run_id`，结构化 JSON 日志记录图规模、求值节点数、拐点、
  耗时与决策原因，可据此重放问题。

## 目录结构

```
src/hvp_service/
  errors.py          # 错误分类（6 类，映射 HTTP 状态码）
  config.py          # 服务器配置与预算（环境变量可调）
  logging_config.py  # 结构化 JSON 日志
  tensor.py          # Layout：扁平向量 ↔ 命名/带形状输入槽的绑定
  ops.py             # 算子注册表：前向、形状推断、反向规则、拐点检测
  graph.py           # 计算图与 GraphBuilder（拓扑序、形状检查）
  autodiff.py        # 双反向：梯度图与 HVP 组合程序构造
  evaluate.py        # 前向求值：溢出/拐点/时间预算诊断
  validation.py      # 向量-布局绑定校验、预算解析
  state.py           # 图存储 + 训练状态（版本化参数点，乐观并发）
  service.py         # 编排层：run_id、日志事件、诊断装配
  schemas.py         # Pydantic 请求/响应模型
  main.py            # FastAPI 入口
tests/               # 独立测试（解析参考 + mpmath 高精度 Hessian）
examples/            # quickstart.py 与 curl 示例
docs/design.md       # 模块契约与数据/错误约定
```

## 安装与运行

```bash
pip install -r requirements-dev.txt   # 锁定版本（见 requirements*.txt）
# 或以包形式安装： pip install -e .

uvicorn hvp_service.main:app --port 8000
# 未安装包时： PYTHONPATH=src uvicorn hvp_service.main:app --port 8000
```

环境变量：`HVP_MAX_GRAPH_NODES`（默认 20000）、`HVP_MAX_EVAL_NODES`
（默认 200000）、`HVP_TIME_BUDGET_MS`（默认不限）。

## 测试

```bash
python3 -m pytest -q     # 42 个测试
```

测试的参考答案**不**来自被测核心：

- 解析二次型 `f = ½xᵀAx + bᵀx`：HVP 应精确等于 `A·v`；
- 非线性 `f = sin(x₀)eˣ¹ + x₀²x₁`：对比手写解析 Hessian；
- mpmath 50 位精度**显式 Hessian**（中心差分）作为外部预言机；
- 另有用服务自身梯度做的中心差分一致性检查（非唯一依据）；
- 零方向、方向线性性、参数共享、非光滑两种策略、状态版本冲突、
  三类资源/计算失败、HTTP 端到端与日志 run_id 回放。

## API 摘要

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | 健康检查 |
| POST | `/graphs` | 注册计算图（节点列表 + 标量输出 id）→ `graph_id` + 输入布局 |
| GET | `/graphs/{id}` | 图信息与训练状态版本 |
| DELETE | `/graphs/{id}` | 删除图 |
| POST | `/graphs/{id}/point` | 设置当前参数点（训练状态），返回递增版本号 |
| POST | `/graphs/{id}/hvp` | 计算值/梯度/HVP |

`/hvp` 请求体：

```json
{
  "point": [0.3, -0.7],            // 扁平向量，长度必须等于布局 size；或用 use_stored_point
  "vector": [1.0, 1.0],            // HVP 方向，同样绑定布局
  "use_stored_point": false,
  "expected_version": null,        // 乐观并发：不匹配 → state_conflict
  "nonsmooth_policy": "reject",    // 或 "subgradient" + "subgradient": 0.5
  "subgradient": 0.0,
  "kink_atol": 0.0,
  "max_eval_nodes": null,          // 只能收紧服务器上限
  "time_budget_ms": null
}
```

响应含 `value`、`gradient`、`hvp`（均按布局顺序扁平化）、`layout`、
`run_id` 与 `diagnostics`（节点计数、耗时、拐点、预算）。

错误统一为 `{"error": {category, message, details, run_id}}`，类别：
`input_validation`(400)、`not_found`(404)、`state_conflict`(409)、
`resource_exhausted`(429)、`computation_failure`(422)、
`nonsmooth_point`(422)。

### 算子一览

`input`、`const`、`add`、`sub`、`mul`、`div`、`neg`、`pow_const`、`exp`、
`log`、`sin`、`cos`、`tanh`、`abs`、`relu`、`sum`、`dot`、`matmul`、
`transpose`、`reshape`、`take`、`scatter`（以及反向规则内部使用的
`sum_to`、`broadcast_to`、`zeros_like`、`*_subgrad`）。

## 示例

```bash
uvicorn hvp_service.main:app --port 8000 &
bash examples/curl_examples.sh     # 建图 → HVP → 非光滑两种策略 → 清理
python3 examples/quickstart.py     # 含训练状态/版本冲突演示
```

## 已知限制

- 仅 float64；稠密逐节点求值，无批处理/GPU；
- 每次 HVP 请求重新构建组合程序（未做程序缓存）；
- 拐点检测基于 `|x| <= kink_atol`，无法发现「几乎不可微」的数值病态区域；
- 次导策略下 HVP 把所选次导当作局部常数（二阶贡献为 0），不建模
  次导集本身随点变化的曲率；
- 时间预算为协作式检查（每 64 个节点一次），不能中断单个超长原子算子；
- `matmul` 仅支持 2-D；`take`/`scatter` 仅支持 1-D；
- 图存储为单进程内存态，重启即失；无鉴权（本地合成夹具定位）。
