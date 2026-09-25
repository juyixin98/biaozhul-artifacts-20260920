# 自适应积分（Adaptive Integration）纯后端

用 Python + NumPy 实现的一维自适应定积分计算库，提供**函数 API** 与
**JSON 接口**（命令行 stdin/文件，无任何网络服务与前端）。核心求积算法
（自适应 Simpson 与 Gauss-Legendre 节点/权）全部自行实现，仅用 NumPy
做对称三对角矩阵特征分解。

面向**小中规模**问题：默认最多 100 000 次函数求值、递归深度 20；
硬上限 1 000 000 次求值、深度 40。

---

## 1. 快速开始

要求 Python ≥ 3.10，仅依赖 NumPy（≥ 1.26）。无需安装，仓库根目录直接运行：

```bash
# 函数 API
python3 -c "
import math
from adaptive_integration import integrate
r = integrate(math.exp, 0, 1, eps_abs=1e-12, eps_rel=1e-12)
print(r.converged, r.value, r.error_estimate, r.n_evals)
"

# JSON 接口（stdin）
echo '{"expression":"exp(-x**2)","a":0,"b":1,"eps_abs":1e-10,"eps_rel":1e-10}' \
  | python3 -m adaptive_integration.cli

# JSON 接口（请求文件，可多个）
python3 -m adaptive_integration.cli examples/request_smooth.json

# 一键跑全部请求样例
bash examples/run_examples.sh
```

## 2. JSON 接口

### 请求字段

| 字段 | 类型 | 必填 | 允许范围 / 说明 |
|---|---|---|---|
| `expression` | string | 是 | ≤200 字符；变量只能是 `x`；`**` 是幂（`^` 为非法）；白名单函数见 §5 |
| `a`, `b` | number | 是 | 有限实数（拒绝 NaN/Infinity/字符串/布尔），跨度 ≤ 1e12；`a>b` 为定向积分自动反号 |
| `eps_abs` | number | 否 | `[0, 1e6]`，默认 1e-8 |
| `eps_rel` | number | 否 | `[0, 1]`，默认 1e-8；`eps_abs+eps_rel>0` |
| `method` | string | 否 | `"simpson"`（默认）或 `"gauss"` |
| `max_depth` | int | 否 | `[1, 40]`，默认 20 |
| `max_evals` | int | 否 | `[1, 1 000 000]`，默认 100 000 |

未知字段一律拒绝；容差、深度、求值预算即**输入范围**的数值化定义。

### 响应

成功（业务收敛）：

```json
{
  "status": "converged",
  "converged": true,
  "value": 0.7468241328124992,
  "error_estimate": 2.9e-11,
  "error_code": null,
  "message": "积分在给定容差与深度/预算限制内收敛。",
  "n_evals": 209,
  "n_intervals": 52,
  "max_depth_reached": 7,
  "method": "simpson",
  "details": {"interval": [0.0, 1.0], "roundoff_hits": 0},
  "request_echo": { ... 回显全部生效参数 ... }
}
```

失败（**不收敛/奇点时 `value` 恒为 `null`，绝不静默给值**）：

| `status` | `error_code` | 含义 |
|---|---|---|
| `failed` | `MAX_DEPTH_REACHED` | 某区间到达深度上限仍未满足局部误差预算（高振荡/极窄峰） |
| `failed` | `EVAL_BUDGET_REACHED` | 函数求值次数超过 `max_evals` |
| `failed` | `SINGULAR_ENDPOINT` | 端点处出现 NaN/Inf（端点奇点，如 `1/sqrt(x)`、`log(x)` 在 0） |
| `failed` | `SINGULAR_INTERIOR` | 区间内部出现 NaN/Inf（内部极点、实数域外求值） |
| `failed` | `FAILED_TOLERANCE` | 兜底：细化停止但总误差估计仍超总预算 |
| `invalid_request` | `INVALID_REQUEST` / `PARSE_ERROR` / `MALFORMED_JSON` / `IO_ERROR` | 请求本身非法 |

失败响应的 `message` 是中文诊断说明，`details` 含奇点坐标、已观测最大
函数幅值、允许深度/预算等机器可读字段。CLI 退出码：正常处理（含业务
失败）为 0；输入无法读取/JSON 非法为 2。

## 3. 算法说明

### 3.1 自适应 Simpson（默认）

- 复合 Simpson 1/3 公式；区间二分细化时**中点求值复用**，每区间每次
  细化只新增 2 次求值。
- 误差估计：Richardson 外推 `err = |S₂−S₁|/15`，外推值
  `S = S₂ + (S₂−S₁)/15`。
- **误差预算递归分配**：子区间获得父区间一半绝对预算（`eps_abs/2`
  逐层下达，QUADPACK 式），局部验收判据
  `err ≤ max(eps_local, eps_rel·|S|)`；返回值的误差估计为各接受
  子区间局部估计之和，顶层再做一次总预算验收。
- 舍入保护：`err ≤ 8·eps_mach·max(h,1)·max|f|` 时视为已到舍入下限，
  予以接受并在 `details.roundoff_hits` 计数、`message` 中提示估计不再
  可靠。
- `max_depth` 硬限制递归层数；`max_evals` 在每次求值前检查预算。

### 3.2 自适应 Gauss-Legendre

- 4 点与 8 点 Gauss-Legendre 规则对；节点与权由 **Golub-Welsch** 过程
  自行生成：用 Legendre 三项递推系数构造对称三对角 Jacobi 矩阵，
  `numpy.linalg.eigh` 求特征值（节点）与首特征向量平方（权）。
- 误差估计取低阶/高阶规则之差 `|G₈−G₄|`（不取 1/15，刻意保守）。
- 递归分配、深度/预算/舍入语义与 Simpson 一致。
- 对高频振荡比 Simpson 稳健（节点不与等距网格结构绑定），但单区间
  成本更高（12 次求值/区间）。

### 3.3 容差的语义与诚实边界

请求 `eps_abs=α, eps_rel=ρ` 的目标是
`|error| ≤ α + ρ·|积分值|`。`error_estimate` 是**经验外推量，不是严格
误差上界**——它在被积函数对求积规则“不可见”时会失效（见 §4）。
这是所有基于嵌套规则差值的自适应方法的共同性质，本库不假装它能
提供数学证明级保证，而是把失效区间明确测出来、写进验收报告。

## 4. 验收研究与“误差估计失效”范围（已实际运行）

运行：

```bash
python3 -m scripts.acceptance_study        # 写 results/ 并打印 Markdown
```

报告与原始数据：`results/acceptance_report.md`、
`results/acceptance_results.json`。实测要点（环境 Python 3.12.3 +
NumPy 2.5.3，日期见 git 提交）：

1. **可解析光滑函数**（x³、exp(x)、exp(−x²)、sin x、1/(1+x²)）容差扫描
   1e-4…1e-12：真实误差随容差下降并满足请求；库内估计通常为真实误差的
   数倍至数千倍（保守）。
2. **高振荡** `sin(2π k x)`：
   - 非整数倍频（k=10.25、100.25）：正确积出非零解析值，真实误差
     1e-13 量级；k≥1000.25 时两种方法都在 100k 预算内**诚实失败**
     （`EVAL_BUDGET_REACHED`），不返回数值；
   - 整数周期：采样点全部落在零点，值偶然正确，但估计（小至 3e-30）
     比真实舍入误差（~5e-13）乐观十几个数量级——隐蔽的失效形态。
3. **Simpson 采样混叠**：`sin(16πx+0.3)` 下 Simpson 仅 5 次求值就声称
   收敛、估计 5e-17，真实值 0 却返回 0.2955；同请求换 `method="gauss"`
   得到 2e-17 的正确结果。
4. **端点窄峰**（Lorentz 峰压在 x=0）：两种规则的节点都落在峰外，
   返回 0.5（精确值≈1）且估计 1e-6~1e-9——**加预算/加深深度无效**，
   属节点族结构性盲区，需用户分段积分或变量代换。中心窄峰则正常
   （深度 20 失败 → 深度 40 成功）。
5. **奇点**：`1/sqrt(x)`、`sqrt(-log x)`、`log(x)@0` →
   `SINGULAR_ENDPOINT`；`1/(x-0.5)` → `SINGULAR_INTERIOR`；
   变量代换（x=t²、x=e^{−u²}）正则化后全部正常收敛。
6. **可去奇点**（sin(x)/x@0）：库不做极限外推，先报端点失败；调用方
   显式补上极限值后正常（Si(1)，真实误差 4e-15）。

> 应对建议：振荡可疑时优先 `gauss`；已知窄峰/奇点位置时分段积分或
> 解析代换；对结果存疑时可用两种方法交叉验证。

## 5. 表达式白名单（安全）

- 常量：`pi e tau`；一元：`sin cos tan asin acos atan sinh cosh tanh
  asinh acosh atanh exp log log2 log10 sqrt cbrt fabs floor ceil erf
  erfc gamma lgamma`（别名 `ln arcsin arccos arctan`）；
  二元：`pow atan2 fmod min max`。
- 运算：`+ - * / % **` 与一元正负号。
- 禁止：属性访问、导入、lambda、推导式、比较/布尔运算、三元表达式、
  字符串/复数/布尔常量、关键字参数、白名单外的一切名称与调用。
- 防护：AST 白名单校验 + `{"__builtins__": {}}` 命名空间执行；
  长度 ≤200、AST 深度 ≤30、字面量绝对值 ≤1e6、字面量统一转 float
  （防止 `9**9**9**9` 大整数 DoS）、幂链嵌套 ≤3。
- 实数域：除零映射为 Inf，`log(负数)`/负数偶次根等映射为 NaN，交由
  核心层分类为端点/内部奇点失败。

## 6. 测试

```bash
python3 -m unittest discover -s tests -v     # 47 个用例
python3 -m pytest tests -q                   # 同样支持
```

覆盖：光滑精度与误差估计对比、定向/退化区间、高振荡收敛与预算失败、
Simpson 混叠（Gauss 对照）、端点/内部/可去奇点、窄峰深度行为、
输入范围校验、解析器注入拒绝与数学错误映射、JSON 字段校验、CLI 端到端
（含退出码）。

## 7. 目录结构

```
adaptive_integration/
  core.py      # 自适应 Simpson / Gauss-Legendre、预算与失败语义
  parser.py    # 安全 AST 表达式解析
  api.py       # JSON 请求 -> 响应（字段校验、错误码）
  cli.py       # stdin/文件命令行
tests/         # 47 个自动化测试
examples/      # 请求样例 + run_examples.sh
scripts/
  acceptance_study.py   # 验收研究（真实误差对比/振荡/窄峰/奇点）
results/       # 实际运行产物（报告、JSON、样例输出）
```

## 8. 已知限制

- 一维、实数、有限积分区间；不做极限外推（可去奇点需调用方处理）、
  不做自动奇点开方/对数检测与代换。
- 误差估计是启发式量，存在 §4 所列的可复现失效区间；需要严格保证的
  场景应使用区间算术等方法，不在本库范围。
- 纯递归实现，深度硬上限 40；规模上限由 `max_evals`（≤1e6）约束。
