# 区间根隔离（Interval Root Isolation）

一维多项式**实根隔离与二分细化**的纯后端服务。给定有理系数多项式
`a0 + a1·x + … + an·x^n`，输出每个不同实根的隔离区间、重数、以及区间内
“恰有一个根”的精确依据。

- 语言/依赖：Python 3.10+，核心仅用标准库 `fractions.Fraction`（**精确有理
  算术，核心算法不含任何浮点**）；测试用 NumPy 做独立交叉验证。
- 无前端、无网络服务，仅提供 Python API 与 JSON 命令行接口。

## 目录

```
rootisolation/
  polyops.py      精确多项式运算：Euclid gcd、无平方因子分解、Sturm 序列、有理根定理
  isolation.py    Sturm + 二分隔离、区间二分细化、Cauchy 根界
  api.py          JSON 请求解析、校验、编排、有界十进制显示
  __main__.py     命令行 JSON 接口（stdin 或文件）
examples/         请求与响应样例
tests/            自动化测试（unittest）
run_tests.sh      一键运行全部测试
```

## 快速开始

```bash
# 文件参数
python3 -m rootisolation --pretty examples/request_irrational.json

# stdin
echo '{"coefficients": [-2, 0, 1], "decimal_digits": 12}' \
  | python3 -m rootisolation --pretty

# 运行测试
./run_tests.sh
# 或：python3 -m unittest discover -s tests -v
```

系数按**低次到高次**给出，可用整数、十进制/科学记数法字符串、`"p/q"`
分数字符串或浮点数。例如 `[-2, 0, 1]` 表示 `x² − 2`。

## 请求字段

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `coefficients` | 数组 | 必填 | 低次到高次的有理系数 |
| `decimal_digits` | int | 10 | 期望的小数位数（区间细化目标宽度 10⁻ᵈ） |
| `target_width` | 正有理数 | 无 | 直接指定目标区间宽度（优先于 decimal_digits） |
| `max_refinement_iterations` | int | 2000 | 每个区间二分细化的最大迭代次数 |
| `max_isolation_depth` | int | 10000 | 隔离阶段最大二分深度 |

## 输出要点

- `status`：`ok` / `invalid_request` / `isolation_limit` /
  `refinement_limit`（CLI 退出码分别为 0 / 2 / 3，内部错误为 1）。
- 每个根：
  - `interval.lower/upper/width`：**精确有理端点与宽度**（分数字符串）；
  - `exact`：是否为精确有理根（此时 `lower == upper`）；
  - `multiplicity`：重数；
  - `decimal_enclosure.guaranteed_digits`：区间宽度**实际**能严格保证的
    十进制位数，取整方向明确，绝不夸大（见“拒绝假精度”）；
  - `evidence.method`：`rational_root_theorem`（精确有理根）、
    `bisection_exact_hit`（二分中点精确命中）或 `sturm_bisection`
    （无理根隔离）。
- `summary`：互异实根数、含重数实根数、非实复根数（含重数）、无平方因子
  分解情况与根数依据。
- `search_bounds`：所有实根所在的 Cauchy 根界。

## 算法

1. **无平方因子分解（square-free factorization）**
   用多项式 Euclid 算法求 `gcd(p, p′)`，迭代剥离出
   `p = lc · ∏ g_i^{m_i}`。每个 `g_i` 无平方因子、两两互素，`m_i` 即其中
   每个根的重数。这一步精确给出**重根与重数**（含偶重根、三重根等）。

2. **有理根精确提取（有理根定理，RRT）**
   对每个无平方因子因子，枚举候选 `p/q`（`p` 整除常数项、`q` 整除首项
   系数）并精确代入；命中的根以退化区间 `(r,r)` 精确报告，并从因子中除尽。
   候选数过多时自动放弃此优化（不影响正确性，剩余根交给下一步）。

3. **Sturm 序列 + 二分隔离**
   对剩余无平方因子多项式构造 Sturm 链
   `p0=p/|lc|, p1=p0′, p_{i+1}=−(p_{i−1} mod p_i)`（余数保持原始符号，
   不做会翻号的归一化）。由 Sturm 定理，`V(a)−V(b)` 给出半开区间
   `(a,b]` 内不同实根个数；扣除右端点根即得严格内部根数。在 Cauchy 根界
   `B = 1 + max|a_k/a_n|` 内递归二分：区间根数 0 丢弃、1 接受、≥2 在中点
   再分。每个被接受区间严格内部恰有一根，且两端点都不是根。

4. **二分细化**
   对每个隔离区间继续用 Sturm 内部计数选择含根的一半，直到宽度
   `≤ 10⁻ᵈ`（或 `target_width`）。中点求值为 0 时立即确认精确根。

## 数值容差与精确性

- 核心算术全部是 `fractions.Fraction`，比较、符号判定、多项式除法均精确，
  因此根数、重数、包含关系是**数学精确**的，不设启发式容差。
- 仅在输出十进制显示时使用 `Decimal`，且取整方向固定：**下界向下截断、
  上界向上截断**，显示区间必然覆盖真实精确区间。
- 保证位数约定：区间宽度 `w ≤ 10⁻ᵈ` ⟺ 中点舍入到小数点后 `d` 位可靠，
  `guaranteed_digits` 取满足该式的最大 `d`。

## 拒绝假精度（失败状态）

- `refinement_limit`：达到 `max_refinement_iterations` 仍未窄到目标宽度时，
  区间按**实际精确端点与真实宽度**返回，`guaranteed_digits` 如实下降，
  并在 `errors.failed_intervals` 列出，绝不声称达到请求的位数。
- `isolation_depth`：达到 `max_isolation_depth` 仍有根未能分开时，返回
  `isolation_limit`，列出未解决区间及估计根数，同时给出已隔离部分。
- 非法输入返回 `invalid_request` 与机器可读错误码。

## 输入范围（限定小中规模）

- 多项式次数：`1 ≤ degree ≤ 100`（常数与零多项式单独处理）。
- 单个系数分子/分母：≤ 4096 个二进制位。
- `decimal_digits`：0..200；`max_refinement_iterations`：1..100000；
  `max_isolation_depth`：1..100000。
- RRT 候选上限 20000（超出则跳过 RRT，纯 Sturm 隔离）。

精确有理算术的系数位宽随细分增长，故明确不面向大规模/高重数病态问题；
上述范围外的请求会被拒绝并说明原因。

## 退化情形

- **零多项式**：`kind="zero_polynomial"`，实根有无穷多个，不做隔离，
  `distinct_real_roots=0` 并在 `root_count_basis` 中说明。
- **非零常数**：`kind="constant"`，实根数为 0。
- **重根/偶重根**：重数由无平方因子分解给出（如 `(x−1)²(x+1)` 报
  1（重数 2）与 −1（重数 1））。
- **相邻根**：如 `x(x−1/2)(x−1)`，三个根都被正确区分（0 与 1/2 间隔仅 0.5；
  测试还覆盖间隔 10⁻⁶ 的相邻根）。

## 测试与验收

`tests/` 覆盖：多项式运算、gcd、无平方因子分解（恒等检验）、Sturm 端点
约定、Cauchy 界、隔离（相邻根、偶/三重根、无理根、无实根、Wilkinson）、
细化、失败状态、输入校验、CLI 退出码，以及用 **NumPy 独立求根**的交叉
核对。另用随机模糊测试（数百个随机整数系数多项式）验证根数与包围关系。

每个非退化隔离区间都在测试中验证：两端点非根、Sturm 严格内部根数恰为 1、
区间两两不重叠、覆盖全部根、宽度与声称的保证位数一致。
