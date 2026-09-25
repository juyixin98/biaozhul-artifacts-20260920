# 区间根隔离（Real-Root Isolation）纯后端

一维多项式**实根隔离**与**二分细化**的纯后端计算库 + JSON 接口。
核心算法（多项式算术、无平方分解、Sturm 链、二分、十进制误差证书）全部自行实现，
**全程在精确有理数（`fractions.Fraction` + Python 大整数）上运行**；
NumPy 只作为 `dtype=object` 数组容器与逐元素算子，**不参与任何浮点判定**。

> 本项目不含任何前端。输入 / 输出均为 JSON。

---

## 1. 它做什么

给定一个单变量多项式
$$f(x)=a_0+a_1x+\cdots+a_nx^n,\qquad a_i\in\mathbb{Q},$$
返回它的**全部不同实根**，对每个根给出：

* 一个**孤立区间** $[a,b]$（精确有理端点），证明其中**恰有一个**不同实根；
  分点恰好是根时给出**精确点根** $\{r\}$；
* 根的**重数**（multiplicity）；
* 一个十进制近似中点 `midpoint` 与一个**经严格证明的误差半径** `radius`，满足
  $$|\,\xi-\texttt{midpoint}\,|\le \texttt{radius},$$
  半径按量子**向上取整**，宁可保守也不伪造精度；
* **计数依据**：Sturm 变号数 $V(a)-V(b)=1$、端点符号、二分深度、Sturm 序列本身。

同时返回按重数计与不按重数计的根数，并处理常数与零多项式。

---

## 2. 方法（算法与正确性依据）

1. **精确有理化输入**。整数与字符串（`"1/3"`、`"-0.25"`、`"1e-6"`）被解析成
   `Fraction`；**拒绝 JSON 浮点字面量**（如 `0.1`），从源头杜绝二进制误差。
2. **Yun 无平方分解**（Q[x] 上，精确 GCD）：
   $$f=c\prod_i p_i^{\,i},\qquad \gcd(p_i,p_j)=1,\ \gcd(p_i,p_i')=1.$$
   每个 $p_i$ 无平方因子，根的重数就是它所在因子的指标 $i$。重乘做一致性校验。
3. **Cauchy 根界** $B=1+\max_{k<n}|a_k/a_n|$，所有根严格落在 $(-B,B)$，
   端点经精确求值确认非根。
4. **Sturm 链**：对无平方 $p$ 取 $s_0=p,\ s_1=p',\ s_{i+1}=-\operatorname{rem}(s_{i-1},s_i)$。
   余数只允许**正比例缩放**（改任何一个符号都会破坏变号数定理）。
   * $V(t)$ = 链在 $t$ 处取值后相邻非零项的变号次数（零跳过）；
   * 端点为根时用单侧极限 $V^-(t)$、$V^+(t)$。
   * **Sturm 定理**：无平方根时开区间内不同根数 $=V(a)-V(b)$。
5. **隔离**：从 $(-B,B)$ 出发，对半分点做**精确**求值与变号数计数，迭代式显式栈二分，
   直到每个叶区间要么含恰 1 个根（且两端非根），要么被标记为“未能隔离”。
   分点精确为零时单独记录为点根，再用 $V^-/V^+$ 隔离其两侧。
6. **细化**：区间既已被 Sturm 证明恰含一个无平方单根，则内部只对 $p$ 自身做**符号二分**
   （单根两侧异号），直到宽度 $\le\epsilon$；保留隔离阶段的变号数作为计数证书。
7. **受控十进制输出**（`decimalize.py`）：量子 $u=10^{-d}$ 覆盖半宽，中点银行家舍入，
   半径 $=\lceil(r+u/2)/u\rceil\,u$，保证覆盖整个区间。

### 数值容差 / 失败状态 / 输入范围

见 `rootisolate/limits.py`：

| 项 | 值 | 越界行为 |
|---|---|---|
| 次数上限 | `MAX_DEGREE = 100` | `degree_out_of_range` |
| 单系数分子/分母位数 | `MAX_COEFF_BITS = 4096` bit | `input_out_of_range` |
| 二分深度默认 / 上限 | `5000 / 10000` | `invalid_input` / `input_out_of_range` |
| `epsilon` 下限 | `1e-200`（默认 `1e-12`） | `epsilon_out_of_range`（**拒绝假精度**） |
| 十进制输出位数 | `MAX_DECIMAL_DIGITS = 256` | `DecimalizationError`（宁可不报数） |

没有任何隐式浮点容差；“是否为零”“选哪一半”全部由精确整数 / 有理数决定。

失败是**显式状态**而非异常猜测：

* `status = "ok"`：全部隔离且细化达标；
* `status = "isolation_depth"`：触顶仍有区间含多个根 —— **不输出这些根的近似**，
  但在 `unresolved` 中给出区间、两端变号数与其中根数（计数仍由 Sturm 严格保证），
  全局根数仍准确；
* `status = "refine_depth"`：根已隔离但未在预算内细化到 `epsilon` —— 区间、计数、
  端点全部有效，`achieved_width` 如实给出实际宽度；
* `status = "zero_polynomial"`：零多项式“根”无定义，不输出区间、根数置 `null`；
* 输入类错误走 `ok:false` + 错误码（见下）。

---

## 3. 安装与运行

只依赖 Python ≥ 3.10 与 NumPy：

```bash
pip install -r requirements.txt      # 仅需 numpy
```

### 命令行

```bash
# 从文件
python -m rootisolate.cli examples/01_basic_quadratic.json

# 从标准输入
cat examples/01_basic_quadratic.json | python -m rootisolate.cli
```

退出码：`0` 成功（含零多项式/常数等合法状态）；`2` 请求或输入非法；`3` 资源限制。

### Python 调用

```python
from rootisolate import solve
result = solve({
    "coefficients": [-2, 0, 1],   # x^2 - 2
    "epsilon": "1e-20",
})
print(result["status"])                       # ok
print(result["real_roots"][0]["approximation"])
```

JSON 字符串封装见 `rootisolate.api.handle_json(text) -> (text, exit_code)`。

---

## 4. 请求格式

```json
{
  "coefficients": [-2, 0, 1],
  "coefficient_order": "ascending",
  "epsilon": "1e-12",
  "max_depth": 5000,
  "bounds": { "lower": "-10", "upper": "10" }
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `coefficients` | 是 | 非空数组。元素为**整数或字符串**；字符串可为 `"2"`、`"-3/4"`、`"0.25"`、`"1e-6"`。全零表示零多项式。**不接受浮点字面量。** |
| `coefficient_order` | 否 | `ascending`（默认，下标 i = x^i）或 `descending`（首项在前） |
| `epsilon` | 否 | 目标区间宽度，正有理数字符串，默认 `1e-12`，最小 `1e-200` |
| `max_depth` | 否 | 隔离与细化各自的二分深度上限，默认 `5000`，最大 `10000` |
| `bounds` | 否 | 仅用于**筛选输出**的闭区间 `[lower, upper]`；隔离始终在严格 Cauchy 界内完成。边界恰为根时标记 `boundary_exact` 并给出精确值 |

### 响应（成功）

顶层 `{"ok": true, "result": {...}}`。`result` 关键字段：

* `status`、`polynomial_kind`（`zero` / `constant` / `non_constant`）；
* `real_roots[]`：
  * `kind`：`isolated_interval` 或 `exact`（精确有理点根）；
  * `multiplicity`、`relation_to_requested_bounds`；
  * `interval`（`lower`/`upper`/`width`，均为分数 + 分子分母）、`sign_at_endpoints`；
  * `approximation`：`midpoint`、`radius`（十进制字符串）、`decimal_places`、
    `certificate`，以及对应的精确 `midpoint_rational` / `radius_rational`；
  * `evidence`：`variations_left/right`、`variation_difference`（恒为 1）、
    `count_assertion`、隔离/细化深度、`achieved_width`、细化状态；
* `root_count_distinct` / `root_count_with_multiplicity`（请求区间内）、
  `global_root_count_*`（Cauchy 界内全部根）；
* `square_free_factorization`：Yun 分解的常数与各首一因子（附系数）；
* `counting_basis`：方法名、定理陈述、算术说明、每个因子的**完整 Sturm 序列**与
  Cauchy 端变号数；
* `unresolved[]`：隔离未决区间（含变号数与根数）、`warnings[]`。

错误响应：`{"ok": false, "error": {"code": ..., "message": ...}}`，错误码包括
`malformed_json`、`invalid_request`、`invalid_input`、`epsilon_out_of_range`、
`degree_out_of_range`、`input_out_of_range`、`resource_limit`。

---

## 5. “拒绝假精度”是如何落实的

* 输入不用浮点；选边不用近似；$\epsilon$ 有下限。
* 每个十进制半径都能在响应里用分数复核：
  `|真中点 - midpoint_rational| + 半宽 <= radius_rational`
  （自动化测试对 100 个随机有理区间逐条验证该不等式）。
* 细化不达标时绝不把当前宽度伪装成已达标：状态置 `refine_depth` 并报告 `achieved_width`。
* 隔离未完成时绝不猜测根数或强行给近似：只给 Sturm 证明过的计数与区间。

---

## 6. 验收覆盖的情形

* **相邻根**：如 $(x-1)(10^6x-(10^6+1))$（两根相距 $10^{-6}$）、$x(x-1)(x+1)$、Legendre P10；
* **偶重根 / 重根**：$(x-1)^2(x+1)^2$（mult 2,2）、$(x-2)^3$、$(x-1)^{10}$、$(x^2-2)^5$、$x^2(x-1)$；
* **无理根**：$x^2-2$（$\pm\sqrt2$，可细化到 200 位并与公开数值一致）；
* **有理系数与点根**：$(x-\tfrac12)(x-\tfrac13)$、$x^3-x$（精确点根）；
* **无实根**：$x^4+1$；
* **常数与零多项式**：分别返回 0 根 / `zero_polynomial`；
* **区间筛选与边界精确根**、**超深 / 超精度 / 超次数 / 浮点输入**的失败与拒绝。

---

## 7. 自动化测试

```bash
python -m unittest discover -s tests -v
```

* `tests/test_core.py`：多项式精确算术、Yun 分解（重乘校验）、Sturm 变号数、
  `sign_at` 对 40 组随机多项式与精确 Horner 的一致性、Cauchy 界严格性；
* `tests/test_isolation.py`：隔离计数不变量、相邻根不相交、细化宽度/异号/计数、
  精确命中、`refine_depth`、十进制半径对 100 个随机区间的严格覆盖；
* `tests/test_api.py`：端到端根数与重数、证书、常数/零多项式、区间筛选与边界根、
  全部输入校验与错误码、`isolation_depth` / `refine_depth` 失败状态、JSON 原生可序列化。

测试本身用 NumPy 浮点求根作为**独立交叉参照**（不作为正确性判据，判据仍是精确证书）。

---

## 8. 目录结构

```
rootisolate/
  limits.py        # 数值容差、深度与规模上限
  polynomial.py    # Fraction + NumPy object 数组的精确多项式算术；整数快速符号求值
  squarefree.py    # Yun 无平方分解（Q[x] 精确 GCD）
  sturm.py         # Sturm 链与变号数（含单侧极限 V-/V+）
  isolate.py       # 迭代式 Sturm 二分隔离 + 单多项式符号二分细化
  decimalize.py    # 经证明的十进制近似（整数 log10，半径向上取整）
  engine.py        # 校验、编排、区间筛选、计数与失败状态汇总
  api.py           # JSON 字符串接口
  cli.py           # python -m rootisolate.cli
tests/             # unittest 自动化测试
examples/          # 请求样例
examples/output/   # 用 CLI 实际生成的响应样例（含失败与拒绝）
```

## 9. 已知边界

* 面向小中规模：次数 ≤ 100。精确算术的成本随系数位数与深度增长，刻意不承诺大规模。
* 复根通过无平方分解处理；不输出复根（仅用 Cauchy 界保证实根被包住）。
* 极接近的一对实根需要约 $\log_2(B/\text{gap})$ 次隔离二分；超预算时返回
  `isolation_depth` 而非近似。
