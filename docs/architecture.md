# 架构与模块契约

## 1. 分层与数据流

```
JSON 请求
   │  (仅接受 int / 十进制或比例字符串；float 在边界即拒绝)
   ▼
numeric_input.parse_request  ── 失败 ──► InputError / StateClosed（见错误契约）
   │  产出 ParsedSystem: Fraction 矩阵 A（m×n）、b（m×k）、digit_budget、want
   ▼
runner.solve_system
   ├─ 每行求 LCM 分母清整：整数增广矩阵 [A_int | b_int]，记录正乘数 mult_i（仅日志/见证用）
   ├─ kernel.eliminate(..., budget_hook=...)          无分数 Bareiss
   │     ├─ 行内容约分（除以全行 gcd，正数，符号不变）
   │     ├─ 前向消元：仅整数；每步精确除断言余数为 0
   │     ├─ 非主元行二次内容约分（得到本原矛盾行）
   │     └─ Fraction 归一化到 RREF + 矛盾行见证表示规范化
   ├─ evidence.extract_solutions    按 RREF 形状读唯一/无穷/矛盾与参数解
   ├─ evidence.independently_verify 用*另一条* Fraction 代码路径对原始 A,b 代回
   └─ evidence.float64_diagnosis    numpy/scipy float64 + mpmath 80 位（标注为近似）
   ▼
JSON 结果信封（含 run_id、rank、solutions、verification、budget、float_diagnosis）
```

关键原则：**浮点诊断只出现在 `float_diagnosis` 字段且带 `"labelled_approximate": true`，
永远不回灌核心**。核心没有任何 `float(...)` 转换路径。

## 2. 模块数据契约

### 2.1 `numeric_input` → `runner`

`ParsedSystem`（frozen dataclass）：

| 字段 | 类型 | 约束 |
|------|------|------|
| `A` | `list[list[Fraction]]` | m≥1, n≥1，矩形 |
| `b` | `list[list[Fraction]]` | m 行、k≥1 列（向量输入归一化为 1 列） |
| `m,n,rhs_count` | int | 与上一致 |
| `digit_budget` | int | 1 ≤ budget ≤ 1,000,000（默认 4096） |
| `want` | str | `solve` / `rank` / `both` |

### 2.2 `runner` → `kernel`

- 整数增广矩阵（逐行清分母，乘的是**正** LCM，不翻符号）；
- `coefficient_cols=n`（主元搜索只覆盖系数块，不覆盖增广列）；
- `input_row_multipliers=mult_i`：告知内核第 i 行由"真实行 × mult_i"得到，
  见证矩阵据此初始化，使终态不变量直接相对**原始有理行**成立；
- `budget_hook(snapshot)`：每个主元步及归一化步后回调。

### 2.3 `kernel` 输出 `EliminationResult`

`rref`（Fraction，全增广宽度）、`rank`、`pivots`、`pivot_columns`、`swaps`、
`witness`（m×m Fraction）、`content_divisors`、`determinant_reduced`（整数）、
`determinant_original`（Fraction）、`step_records`。

**核心不变量（测试逐元素验证）**：

```
RREF[i][j] == Σ_t witness[i][t] · true_input[t][j]      对所有 i,j
```

其中 `true_input` 是清分母之前的原始有理增广行。由此：

- RREF 中系数全零的行 i，其见证 `y = witness[i]` 满足 `yᵀ·A = 0`；
- 若该行右端 `c ≠ 0`，则同一 `y` 满足 `yᵀ·b = c ≠ 0`，即为矛盾的可代回证据。

行列式（方阵满秩）：`det(真实 A) = det(内容约分后整数矩阵) · Πg_i / Πmult_i`，
其中 Bareiss 最后主元等于内容约分矩阵行列式乘以 `(-1)^交换次数`。

## 3. 无分数消元与增长控制

1. **行内容约分**：消元前把每行除以其所有元素的 gcd。这是处理"大公因子"矩阵的主要手段；
   除数恒为正，符号不变。
2. **Bareiss 前向消元**（Sylvester 整除恒等式）：

   ```
   a'_ij = (pivot · a_ij − factor · pivot_row_j) / previous_pivot
   ```

   全程整数。注意即使某行 `factor == 0` 也**不能跳过**更新——该行仍需乘上
   `pivot/previous_pivot`，否则会破坏后续步的精确整除不变量（本工程曾由此抓到一个真实 bug，
   回归测试见 `test_kernel.py` 的差分对拍）。每步断言余数为 0，违例即 `computation_failed`。
3. **主元选择**：取前沿下方绝对值最小的非零元（控制增长）；需要时仅做**行交换**并记录，
   从不对行乘 −1，因此行列式符号只可能因交换翻转。
4. **归一化**：在整数上三角结果上用 Fraction 自下而上归一主元为 1、消去上方元素。
5. **矛盾行规范化**：非主元行缩放到"整数本原、首非零分量为正"的见证表示；
   RREF 行同步缩放，不变量保持。这只统一 0=c 行的表示，不改变任何判定。

## 4. 解分类与参数化

设主元列集合 P、自由列 F。

- **矛盾**（任一右端）：存在系数全零、右端非零的 RREF 行 → 返回见证 `y`；
- **一致**：取自由变量为 0，主元变量读自主元行右端 → 特解 `x0`；
  每个自由列 f 生成一个零空间向量（f 处为 1，主元处为 RREF 对应系数的负值）。
  `F` 为空即唯一解，否则无穷多解，返回
  `x = x0 + Σ_{k} t_k · v_k`。

多右端矩阵 `b` 时，各右端**独立分类**（同一系统可以一个右端无穷多解、另一个矛盾）。

## 5. 独立证据（不依赖被测核心）

`evidence.independently_verify` 不复用 kernel 任何中间量，直接对**原始** `A, b`：

- 对每个特解重算 `A·x0 − b`，必须逐项为 0；
- 对每个零空间向量重算 `A·v`，必须逐项为 0；
- 对矛盾见证重算 `yᵀ·A`（必须全 0）与 `yᵀ·b`（必须非 0）；
- 任一不符 → `computation_failed`（服务不会返回未通过自检的结果）。

测试层另有 `tests/oracle.py`：朴素 Fraction Gauss-Jordan、独立的 Aᵀ 零空间见证推导、
分数高斯行列式、mpmath 80 位行列式与 numpy float64 秩。期望值由这些独立路径/手算产生，
**不由被测核心自证**。

## 6. 预算与可诊断中间进度

- 预算单位为"单个中间整数的十进制位数上界"（`decimal_digits_bound`，保守安全）。
- 钩子在**第 0 步（原始输入）**及每个消元/归一化步后触发；超限即抛 `BudgetExhausted`：
  - HTTP 422，`error="budget_exhausted"`；
  - `details.progress` 含：`phase`、`step`、`column`、`rank_so_far`、`previous_pivot`、
    `pivots`、`swaps`、`max_decimal_digits`、完整 `matrix`（精确整数字符串）、
    `reason`、`observed_max_digits`；
  - 计算在仍然精确的状态下停止，系统中**不存在**浮点回退分支。

## 7. 运行日志与重放契约

日志为 JSONL（路径由环境变量 `RATIONALSVC_RUN_LOG` 决定，默认 `run_log.jsonl`），
每行一个事件，均带 `run_id` 与 UTC `ts`：

| 事件 | 关键字段 |
|------|----------|
| `started` | 精确 A、b（Fraction 字符串）、shape、budget、want |
| `denominators_cleared` | 每行乘数、整数增广矩阵 |
| `checkpoint` | phase/step/column、rank_so_far、swaps、当前最大位数 |
| `budget_exhausted` | limit、observed、完整 progress |
| `completed` | rank、各右端分类、observed_max_digits |
| `error` | 错误类别、message、details |

重放（`GET /runs/{id}` 或 `python -m rationalsvc.replay`）从 `started` 事件重建请求并
**重新计算**（而非回显缓存结果），可用 `--digit-budget` 覆盖以复现耗尽。

## 8. 错误契约

所有非 2xx 响应体形如：

```json
{"error": "<code>", "message": "<human readable>", "details": {...}, "run_id": "..."}
```

闭合类别见 `errors.ErrorCode` 与 README 表格。输入错误、状态冲突、资源耗尽、
计算失败四族通过 `error`（及不同 HTTP 状态）可程序化区分；`computation_failed`
的 `details` 携带异常类型与触发不变量的具体数值（如 Bareiss 余数）。
