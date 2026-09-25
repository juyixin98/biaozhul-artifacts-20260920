# 运行记录（RUNLOG）

本文件如实记录开发过程中实际执行的命令、结果，以及**曾经未通过 / 出现错误的项与修复**。
环境：Ubuntu (Linux 6.8.0)，Python 3.12.3，NumPy 2.5.3。纯后端，无前端。

## 1. 环境确认

```text
$ python3 --version && python3 -c "import numpy; print('numpy', numpy.__version__)"
Python 3.12.3
numpy 2.5.3
```

预先验证 NumPy `dtype=object` 上的 `np.convolve` 与向量化 Horner 对
`fractions.Fraction` 为逐元素精确运算（无浮点），确认该算术底座可行。

## 2. 开发中实际出现并修复的错误（未通过项）

按出现顺序记录，均在最终测试前修复并有回归测试覆盖：

1. **Yun 无平方分解迭代写错**：`z` 的更新漏了 `(z - y')` 的减法，
   直接用 `z/yk`，在 `x^2-2` 上抛 `polynomial division is not exact`。
   修正为标准 Yun 步骤 `z <- (z - y')/yk`。
   *回归覆盖：`tests/test_core.py::TestSquareFree`（每个分解重乘回原多项式）。*

2. **Sturm 链在除尽余数为零时未停止**，对常数余项继续取余导致
   `Fraction(1, 0)`。增加 `is_zero` 终止；最终余数是常数，链正常结束。

3. **`primitive_positive` 缩放关系错误**：去整数内容时把公分母 `D` 也除了，
   对常数 `2` 得到 `D=0`。重新定义返回值为正有理缩放因子 `s=h/D`（`a = s·g`），
   统一更新 squarefree / sturm 两处调用，保证缩放恒正、不改任何符号。

4. **Python 递归上限（RecursionError → resource_limit）**：隔离递归深度可达 5000，
   超过默认 1000。**重写为迭代式显式栈**（不依赖 `setrecursionlimit`）。

5. **`(l+r)/2` 产生 float**：端点为 int 时 Python `/` 是真除法，会静默引入浮点，
   破坏“全程精确”。统一改为 `(l+r) * Fraction(1,2)`；用 grep 排查所有 `/2`。

6. **隔离收叶判断顺序错误**：把“深度检查”放在“n==1 收叶”之前，导致已经只含
   单个根的区间在到达深度上限时被误报为未决，`x^2-2` 错误地返回
   `isolation_depth`。改为“n==1 且两端非根即无条件收叶”。

7. **点根计数重复扣除**：端点根区间误用额外标志又减一次，出现
   `factor count mismatch 1+-1!=2`。统一为单侧极限语义 `V+ / V-`（它们本就排除
   端点根），区间内部根数恒为 `vl - vr`，并让未决记录携带 `interior_count`，
   引擎断言 `隔离叶数 + 未决内部根数 == Sturm 总数`。

8. **高精度 Horner 挂起（性能，被 120s 超时杀掉）**：细化后用大分母 Fraction
   做 `P.horner` 取符号，GCD/幂运算爆炸。两处优化：
   * 细化阶段只对单多项式做**整数符号求值** `sign_at`（通分后整数 Horner，只取符号）；
   * Sturm 变号数、判零、端点符号全部改走 `sign_at`，不再构造大 Fraction。
   之后 `x^2-2 @ 1e-200` 约 0.03s。

9. **十进制量子选择死循环/挂起**：初版用 `float(log10)` 猜测位数再用 `while`
   纠正，方向写反后对负 d 计算 `10**巨大值` 挂死（faulthandler 定位）。
   重写为**纯整数** `floor(log10)`（bit_length 给初值 + 整数幂校正）。

10. **半径向下取整 → 假精度（最重要）**：`rad_units` 误对绝对误差界直接 `ceil`
    （≈1.06e-8 得 1），而不是除以量子 `u=1e-8` 再 `ceil`（应得 2）。
    这会让宣传的 `midpoint ± radius` 覆盖不到区间端点。修正为
    `radius = ceil((r + u/2)/u) · u`。
    *回归覆盖：`test_radius_covers_interval_exactly` 对 100 个随机有理区间逐条验证
    `|真中点-mid_dec| + 半宽 <= radius`；端到端测试再验证一次。*

11. **测试自身的期望错误（非产品 bug）**，记录以示区分：
    * 左/右极限变号数测试期望写反，按 Sturm 定义改正；
    * 浮点交叉参照把**低次零系数（真根 x=0）**与高次零、以及不同根数/带重数根数
      混淆，改用本库去高次零后的系数并分别比较 distinct / weighted 两个口径。

## 3. 最终自动化测试

```text
$ python3 -m unittest discover -s tests -v
...
Ran 53 tests in 0.081s
OK
```

53 个测试全部通过（`tests/test_core.py`、`tests/test_isolation.py`、
`tests/test_api.py`）。`python3 -m compileall rootisolate tests` 通过。

## 4. 独立交叉验证（NumPy 浮点仅作参照，正确性判据仍是精确证书）

* 150 个随机整数系数多项式（次数 1–13，刻意包含低次零 = 根 0、含重根）：
  不同根数与带重数根数**全部**与 `numpy.roots` 一致；每个孤立区间都覆盖对应
  NumPy 实根、且区间两两不交。结果：`trials=150 fails=0`。
* 指定案例：

| 多项式 | distinct / weighted | 结果 |
|---|---|---|
| `x^2-2` | 2 / 2 | ±√2，可细化到 200 位且与公开数值一致 |
| `x^2` | 1 / 2 | 偶重根 0，精确点根 |
| `(x-1)^2(x+1)^2` | 2 / 4 | mult 2,2 |
| `(x-2)^3` | 1 / 3 | mult 3 |
| `(x-1)(x-2)(x-3)(x-4)` | 4 / 4 | max_depth=1 时确定性 `isolation_depth`，全局计数仍 4 |
| `x^4+1` | 0 / 0 | 无实根 |
| `(x-1/2)(x-1/3)` | 2 / 2 | 有理系数，精确点根 + 区间 |
| `x(x-1)(x+1)` | 3 / 3 | 三个精确点根 |
| 两根相距 1e-6 的整数多项式 | 2 / 2 | 相邻根成功分离，区间不交 |
| Legendre P10（缩放） | 10 / 10 | 10 个相邻无理根全部隔离 |
| 非零常数 `7` | 0 / 0 | constant |
| 零多项式 `[0,0,0]` | null / null | `zero_polynomial`，不定义根 |

性能（本机，默认参数）：Wilkinson n=20 约 0.14s；随机次数 50 约 0.13s；
随机次数 100 约 0.51s；`(x-1)^10`、`(x^2-2)^5` 毫秒级。

## 5. 失败状态 / 输入拒绝的实际演示

```text
examples/07 (max_depth=1, 4 根)  -> exit 0, status=isolation_depth，
                                    unresolved 给出 [0,51] 内 4 根（V=4−0），
                                    global_root_count_distinct 仍为 4
examples/08 (epsilon=1e-300)     -> exit 2, epsilon_out_of_range（拒绝假精度）
examples/09 (coefficient 0.1)    -> exit 2, invalid_input（拒绝浮点字面量）
```

命令（退出码与响应均已实跑）：

```bash
for f in examples/0*.json; do python3 -m rootisolate.cli "$f"; done
# 成功案例退出码 0；08/09 退出码 2
```

## 6. 已知限制（见 README 第 9 节）

* 仅承诺小中规模：次数 ≤ 100、系数 ≤ 4096 bit、深度 ≤ 10000、ε ≥ 1e-200。
* 只输出实根；复根经无平方分解处理重数。
* 根过于接近导致隔离/细化超预算时，返回 `isolation_depth` / `refine_depth`，
  不猜测、不伪造，计数与已得区间仍严格成立。
