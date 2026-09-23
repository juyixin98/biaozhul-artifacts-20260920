# Stable Pool Quoter（双资产稳定池离线报价服务）

纯后端 API：对双资产稳定池做离线报价。核心求解路径使用**精确整数运算**（任意精度），
有界迭代牛顿法为主求解器，**独立二分求根**作为参考对照；任何未收敛或求解器不一致的
结果都**不会**返回可成交报价。

> 本项目的不变量是 StableSwap *风格* 的自研定义，用于数值求解演示，
> **不宣称**逐字节复刻任何已部署协议。

## 数学定义

池内含 2 种资产，余额归一化为 18 位小数定点整数 `x, y`。放大系数 `A`（整数），
`Ann = A * n^n = 4A`（n=2）。不变量常数 `D` 满足：

```
Ann * (x + y) + D  ==  Ann * D + D^3 / (4 * x * y)
```

等价地，`D` 是 `f(D) = D^3/(4xy) + (Ann-1)*D - Ann*(x+y) = 0` 的唯一正根
（A ≥ 1、x,y > 0 时 f 严格单调递增，根唯一）。

**放大参数范围**：`A ∈ [1, 5000]`（本项目自定义的支持范围，超出即拒绝，HTTP 422）。

**报价（求 y）**：给定对方余额 `x` 与不变量 `D`，解

```
h(y) = y^2 + (b - D) * y - c = 0,   b = x + D // Ann,   c = D^3 / (4 * Ann * x)
```

输出 `dy = y_old - y_new`。

### 求解器

| 求解器 | 用途 | 收敛判据 |
|---|---|---|
| 牛顿法（有界迭代） | 主求解器（求 D、求 y） | 相邻迭代差 ≤ 1（归一化整数单位） |
| 二分求根 | 独立参考（求 y），与牛顿法无共享状态 | 区间宽度 ≤ 1 |

- 迭代上限：默认 255 次/求解器，客户端可通过 `max_iter` 调整，硬上限 1000。
- **极限环处理**：整数向下取整可能使牛顿迭代在根附近进入周期极限环（实测场景：
  储备 `1e24` 对 `1`）。求解器显式检测极限环，并取环中不变量残差最小的成员，
  返回中 `converged_via` 标记为 `"cycle"`（正常收敛为 `"delta"`）。
- 报价可成交（`executable=true`）的充要条件：D 求解收敛 **且** y 求解收敛 **且**
  二分参考收敛且与牛顿结果相差 ≤ 4 个归一化单位。否则 `quote` 为 `null`。

### 精度与舍入

- 各资产先按各自 `decimals` 归一化到 18 位小数定点（纯乘法，无精度损失）。
- 求解路径全程为 Python 任意精度整数运算，**无浮点**；所有除法为向下取整。
- 最终输出按输出资产精度反归一化，**显式向下取整**（`//`），不足 1 个原生单位的
  粉尘被丢弃，不会被静默入账。
- NumPy `longdouble`（x86-64 Linux 上为 80 位扩展精度）仅用于不变量残差的
  独立诊断（`invariant_residual_float`），不参与报价数值。
- 精确残差以分数形式返回（`invariant_residual_exact`，"p/q"），无任何舍入。

## 项目结构

```
app/
  pool.py         # 不变量、牛顿/二分求解器（纯整数）、归一化与舍入
  service.py      # 报价流水线：归一化 → 解 D → 解 y → 二分对照 → 舍入
  diagnostics.py  # NumPy 高精度残差诊断（仅报告）
  schemas.py      # Pydantic 请求/响应模型（金额一律为十进制字符串）
  main.py         # FastAPI 入口
tests/            # pytest 自动化测试（31 个用例）
examples/         # 示例请求体
requirements.txt  # 锁定依赖
```

## API

- `GET  /v1/health` — 健康检查
- `GET  /v1/metadata` — 不变量定义、参数范围、求解器与舍入策略
- `POST /v1/quote` — 报价。请求示例见 `examples/quote_balanced.json`：

```json
{
  "reserves": ["1000000000000", "1000000000000000000000000"],
  "decimals": [6, 18],
  "amplification": 100,
  "token_in": 0,
  "amount_in": "1000000",
  "max_iter": 255
}
```

响应要点：`status` ∈ `ok | not_converged | solver_disagreement | invalid_pool`；
`executable=false` 时 `quote` 必为 `null`；`newton` / `bisection_reference`
分别报告收敛状态、迭代次数、残差与收敛方式。

## 本地启动

```bash
pip install -r requirements.txt
python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

## 验收命令

```bash
# 1. 自动化测试（极端失衡、小额输入、零储备、参数边界、迭代上限、牛顿vs二分对照等）
python3 -m pytest tests/ -v

# 2. 健康检查与元数据
curl -s http://127.0.0.1:8000/v1/health
curl -s http://127.0.0.1:8000/v1/metadata | python3 -m json.tool

# 3. 平衡池报价（约 1:1，含完整求解器诊断）
curl -s -X POST http://127.0.0.1:8000/v1/quote \
  -H 'Content-Type: application/json' \
  -d @examples/quote_balanced.json | python3 -m json.tool

# 4. 失衡池报价
curl -s -X POST http://127.0.0.1:8000/v1/quote \
  -H 'Content-Type: application/json' \
  -d @examples/quote_imbalanced.json | python3 -m json.tool

# 5. 迭代上限：max_iter=1 必然不收敛，且不得返回可成交报价
curl -s -X POST http://127.0.0.1:8000/v1/quote \
  -H 'Content-Type: application/json' \
  -d '{"reserves":["1000000000000","1000000000000000000000000"],"decimals":[6,18],
       "amplification":100,"token_in":0,"amount_in":"1000000","max_iter":1}'
```

## 测试覆盖

- 平衡池 D 精确值（D = x + y）与约 1:1 报价
- 极端失衡（1 wei 对 1e30 归一化单位；极限环回归测试）
- 小额输入（1 wei）
- 零储备（全零 / 单侧零 → `invalid_pool`，不可成交）
- 放大参数边界（A=1、A=5000 接受；A=0、A=5001 拒绝 422）
- 迭代上限（max_iter=1 → `not_converged`，`quote=null`；硬上限 1000）
- 牛顿法与二分参考一致性（含随机化 NumPy 参数扫描）
- 交换后不变量残差可忽略（精确分数校验）
- 归一化/反归一化往返与粉尘向下取整
- 非法输入（负数、浮点、非字符串金额、越界索引/精度）→ 422

## 诚实性说明

- 所有计算真实执行：无缓存结果、无预计算答案、无跳过求解的捷径。
- 未收敛、求解器不一致、池状态非法时返回对应 `status` 且 `quote=null`，绝不返回
  可成交报价。
- 已知边界：极端失衡角落中整数 D 对 y 的条件数很大（dy/dD 可达 ~1e9），此时以
  不变量残差而非绝对余额误差衡量解的质量；响应中的残差字段如实反映这一点。
