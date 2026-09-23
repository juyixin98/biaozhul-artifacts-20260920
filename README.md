# 稳定池数值求解 — 双资产稳定池离线报价 API

纯后端（Python + FastAPI + NumPy）实现的双资产稳定池离线报价服务。
所有定价使用 **80 位 `Decimal` 高精度**；资产精度归一化后求解，最终整数
输出采用**显式向下取整（ROUND_DOWN）**。主求解器为**有界保护牛顿迭代**，
并由一条**独立的纯二分求根**参考实现交叉校验；未收敛或守卫未通过的
报价**一律不可成交**（`tradable=false` 且不携带任何输出金额）。

> 声明：本项目实现的是自定义的 stable-swap 家族曲线，用于演示与研究，
> **不宣称完全复刻任何已部署协议**（包括其参数化、手续费或取整细节）。

## 不变量与参数（完整定义）

归一化单位下（`x, y > 0` 为两资产储备，`A` 为放大参数）：

```
D^3 / (4·x·y) = 2·A·(x + y) + D·(1 - 2·A)
```

等价根式：`f(D) = D^3/(4xy) + (2A-1)·D - 2A·(x+y) = 0`

- **放大参数范围**：整数 `A ∈ [1, 100000]`（请求越界返回 422）。
  - `A → 0` 是恒定乘积（`x·y` 恒定，`D = 2√(xy)`）的极限；
  - 整数域 `A ≥ 1` 全部位于稳定币区，`A` 越大曲线在平衡点附近越平（趋近恒定和）；
  - 平衡点 `x = y` 时恒有 `D = x + y`。
- **兑换**：保持 `D` 不变。输入端新储备 `x' = x + a`（全额输入入池），
  解二次方程求输出端新储备 `y'`：

  ```
  g(y') = 8·A·x'·y'^2 + [8·A·x'^2 + 4·x'·D·(1-2A)]·y' - D^3 = 0
  ```

  毛输出 `gross = y - y'`。
- **手续费**：对**输出端**收取，`fee = gross · fee_bps / 10000`，
  `fee_bps ∈ [0, 10000]`（默认 4 = 0.04%）。净输出 `net = gross - fee`。
- **归一化与舍入**：整数代币单位 → 归一化 `amount / 10^decimals`（`decimals ∈ [0,36]`），
  80 位 Decimal 求解，最终输出 `floor(net · 10^decimals_out)`（向零取整，
  永不超额支付）。
- **后置守卫**：用真实整数后置储备 `(x+a, y-out)` 重算 `D`，与含费精确参考态
  `(x+a, y-net)` 的 `D` 比较，偏差须落在取整灰尘容差内（双向），否则
  `INVARIANT_VIOLATION`，报价不可成交。

## 求解器

| 求解器 | 角色 | 策略 | 收敛契约 |
|---|---|---|---|
| `solve_d` / `solve_y` | 生产 | 保护牛顿：原始牛顿步优先，越界/停滞时退化为二分中点；凸增函数从上方逼近单调有界 | 相对步长 < 1e-50 且相对残差 < 1e-50 |
| `solve_d_bisect` / `solve_y_bisect` | 独立参考 | 纯二分，括号 `(0, hi)` 独立构造（y 用全区间 `(0, D]`） | 括号宽度 < 1e-65（绝对） |

两条路径**故意独立**（不同括号、不同收敛尺度），保证微观交易（如 1e-30
归一化单位的输入）不会被大储备“吞掉”而漏检。每次报价同时返回：收敛状态、
迭代次数、二分回退次数、相对残差、牛顿/二分根一致性（相对 + 绝对）。

- 迭代上限：`max_iterations` 默认 64，硬上限 512（超限请求被钳制）。
- 未收敛 ⇒ `tradable=false`，`error.code ∈ {D_NOT_CONVERGED, Y_NOT_CONVERGED}`，
  **不返回任何可成交金额**。

## 快速开始

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock   # 锁定依赖（pip freeze 生成）

# 启动服务
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000

# 健康检查与不变量说明
curl -s http://127.0.0.1:8000/health
curl -s http://127.0.0.1:8000/invariant | python3 -m json.tool

# 报价（示例见 examples/）
curl -s -X POST http://127.0.0.1:8000/quote \
  -H 'content-type: application/json' \
  -d @examples/quote_balanced.json | python3 -m json.tool
```

## 测试与验收

```bash
# 自动化测试（不变量恒等式、双求解器一致性、极端失衡、小额输入、
# 零储备、参数边界、迭代上限、舍入、API 层）
.venv/bin/python -m pytest tests/ -q

# 一键验收：测试 + 启动真实服务 + 示例报价冒烟
bash scripts/acceptance.sh
```

## API

### `POST /quote`

请求（金额一律为整数字符串或整数，避免 JSON 浮点精度损失）：

```json
{
  "reserve_in_units": "100000000",
  "reserve_out_units": "100000000",
  "amount_in_units": "10000000",
  "decimals_in": 6,
  "decimals_out": 6,
  "amp": 20,
  "fee_bps": 4,
  "max_iterations": 64,
  "include_reference": true
}
```

成功响应（节选）：

```json
{
  "quote_id": "<sha256 hex>",
  "tradable": true,
  "quote": {
    "amount_out_units": "9948196",
    "amount_out_normalized": "9.948196...",
    "gross_out_normalized": "9.952177...",
    "fee_out_units": "3980",
    "spot_price_before": "1",
    "effective_price": "0.9948196",
    "price_impact_bps": "51.78..."
  },
  "solver": {
    "d_newton":    {"iterations": 1, "converged": true, "residual_rel": "0E-80", ...},
    "d_bisection": {"iterations": ..., "converged": true, ...},
    "y_newton":    {"iterations": 8, "converged": true, ...},
    "y_bisection": {"iterations": ..., "converged": true, ...},
    "roots_agree": true,
    "root_agreement_rel": "2.97E-68",
    "post_trade": {"d_drift_rel": "1.57E-9", "fee_surplus_rel": "2.0E-5", "invariant_ok": true}
  }
}
```

不可成交响应（HTTP 200，`tradable=false`，无 `quote`）的 `error.code`：

| code | 含义 |
|---|---|
| `D_NOT_CONVERGED` / `Y_NOT_CONVERGED` | 迭代上限内未收敛 |
| `ROOT_MISMATCH` | 牛顿根与独立二分根不一致 |
| `ZERO_PAYOUT` | 输出向下取整后为 0（交易过小） |
| `INSUFFICIENT_LIQUIDITY` | 输出端无正产出 |
| `INVARIANT_VIOLATION` | 整数执行的后置 D 守卫未通过 |
| `REFERENCE_FAILED` | 独立参考求解器失败 |

请求级错误（HTTP 422）：`ZERO_RESERVE`（零/单边储备）、`ZERO_INPUT`（零输入）、
以及 amp/decimals/fee 越界等 schema 校验。

### `GET /invariant`

返回不变量方程、放大参数范围、精度/容差/取整规则的完整机器可读说明。

### `POST /diagnostic/sweep`

NumPy float64 向量化曲线扫描（`reserve_in, reserve_out, amp, n_points, max_fraction`）。
**明确标注 `float64-diagnostic`，仅供可视化参考，绝不作为可成交报价**——
定价路径只使用 80 位 Decimal。

## 项目结构

```
app/
  main.py            FastAPI 应用与端点
  schemas.py         请求/响应模型（BigIntStr 整数字符串）
  quotes.py          报价引擎：校验、费用、守卫、响应组装
  core/
    config.py        精度、容差、参数边界、迭代上限
    precision.py     Decimal 上下文与归一化/取整
    invariant.py     不变量、保护牛顿、独立二分
    sweep.py         NumPy float64 诊断扫描（非定价）
tests/               pytest 套件（151 例）
examples/            示例请求 JSON
scripts/acceptance.sh  一键验收
requirements.lock    锁定依赖
```

## 已验证的边界行为（测试覆盖）

- 极端失衡（储备比至 1e30）：牛顿与二分一致，D 求解有界收敛；
- 小额输入（1e-60 归一化单位）：微观输出被正确分辨，不被大储备吞没；
- 零储备/单边池：422 `ZERO_RESERVE`；零输入：422 `ZERO_INPUT`；
- 参数边界：`amp ∈ {1, 100000}` 可成交；`amp=0 / 100001` 拒绝；
- 迭代上限：`max_iterations=1` 强制不收敛，返回不可成交且无金额；
- 天文数字输入（1e30）：被后置不变量守卫拒绝；
- 整数舍入：输出恒为向下取整，永不超额支付。
