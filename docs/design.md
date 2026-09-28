# 设计：模块契约与数据/错误约定

## 分层

```
HTTP (main.py, schemas.py)
  └─ 编排 (service.py)           — run_id、日志事件、诊断装配
       ├─ graph.py               — 建图：拓扑序、形状推断、输入布局
       ├─ autodiff.py            — 双反向程序构造（纯图变换，不求值）
       ├─ evaluate.py            — 前向求值（溢出/拐点/时间预算）
       ├─ validation.py          — 向量-布局绑定、预算解析
       └─ state.py               — 图存储 + 版本化训练状态
```

支撑模块：`tensor.py`（布局）、`ops.py`（算子注册表）、`errors.py`
（错误分类）、`config.py`（预算）、`logging_config.py`（JSON 日志）。

## 数据契约

- **Layout**（`tensor.py`）是唯一的形状权威：图声明有序输入槽
  `(name, shape)`，所有点/方向/梯度/HVP 均为长度等于 `layout.size` 的
  扁平 float64 向量，按槽顺序切片。长度不符或非有限 →
  `input_validation`，绝不静默 reshape。
- **Node/Graph**（`graph.py`）：节点只能引用更早的节点（拓扑序由构造
  保证，无环）；输出必须是标量。图注册后不可变。
- **HvpProgram**（`autodiff.py`）：一次组合求值同时产出值、梯度、HVP；
  方向 `v` 与次导值在程序构造时烘焙为常量/构建选项。
- **Op**（`ops.py`）：每个算子声明 `forward`、`shape_fn`、`backward`、
  可选 `kink_fn`。`backward` 用同一算子集建节点，这是双反向可行的根因；
  返回 `None` 表示该输入的贡献恒为零（如 `zeros_like`、次导映射）。
- **训练状态**（`state.py`）：图不可变，参数点可变；每次 `set_point`
  递增 `point_version`。`use_stored_point + expected_version` 提供乐观
  并发：版本不符 → `state_conflict`，调用方不会在过期迭代点上静默
  计算 HVP。

## 错误契约

所有预期失败都是 `ServiceError`，携带 `category`、人类可读 `message`、
结构化 `details` 与 `run_id`（如适用）：

| category | HTTP | 触发 |
|---|---|---|
| `input_validation` | 400 | 未知算子、环引用、非标量输出、向量长度/有限性、次导越界、非法预算 |
| `not_found` | 404 | 未知或已删除的 graph_id |
| `state_conflict` | 409 | 版本不匹配、无存储点却用 use_stored_point |
| `resource_exhausted` | 429 | 图节点/求值节点/时间预算超限（details 含观测值与上限） |
| `computation_failure` | 422 | 中间结果非有限（溢出、定义域错误），details 含节点与算子 |
| `nonsmooth_point` | 422 | reject 策略下命中拐点，details 含拐点位置 |

## 可重放性

每次 HVP 请求生成 `run_id`，日志事件 `hvp_start`（输入规模、策略、
预算）→ `hvp_program_built`（前向/梯度/总节点数）→ `hvp_done`（求值
节点数、耗时、拐点数、函数值）或 `request_failed`（类别与原因）均带
`run_id`。响应 `diagnostics` 内联同样的关键中间状态，测试
`test_api.py::test_run_id_appears_in_structured_logs` 对此断言。

## 正确性论证要点

- 反向规则按输入槽返回贡献，多消费者通过 `add` 节点累加 —— 共享子图
  前向算一次、伴随恰好求和一次，不重不漏；
- 第二次反向传播处理的是第一次反向**建出来的图**，复用同一套规则，
  因此二阶项（如 `exp` 的 `g·out`、`tanh` 的 `1-out²`）自然出现；
- 测试用独立于核心的解析解与 mpmath 50 位精度显式 Hessian 交叉验证，
  避免「核心与自己一致」的循环论证。
