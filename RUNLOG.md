# RUNLOG：实际运行记录

日期：2026-09-23。以下命令与输出均为实际执行所得；过程中发现的问题与修复
如实记录在“开发过程中发现并修复的问题”一节。

## 1. 环境

```
$ python3 --version
Python 3.12.3
$ python3 -c "import numpy; print(numpy.__version__)"
2.5.3
$ python3 -c "import pytest; print(pytest.__version__)"
9.1.1
```

依赖：仅 NumPy（核心算法）；pytest 仅用于测试。无任何 LP 求解器依赖。

## 2. 自动化测试

```
$ python3 -m pytest
........................................................................ [ 71%]
.............................                                            [100%]
101 passed in 1.21s
```

测试文件：

* `tests/test_simplex_core.py`：小整数手算例、Phase I、负右端、变量上下界、
  不可行 Farkas 证书、无界射线、退化、重复/成比例/冗余约束、Beale 循环、
  精确算术（fractions.Fraction）再现循环；
* `tests/test_crosscheck.py`：40 组随机小整数 LP 与顶点枚举参考解对照、
  随机不可行证书批量核验、多最优解情形；
* `tests/test_json_cli.py`：JSON 两种写法、null 上界、非法请求字段路径、
  NaN 拒绝、选项校验、CLI 文件/stdin/退出码端到端；
* `tests/test_edge_cases.py`：零费用、零列、0=0/0=1 退化等式、
  相依冗余等式、零右端、下界平移冲突、规模/数值范围/维度校验。

**当前无未通过项。**

## 3. 六个样例的实际运行（退出码均为 0）

```
01_optimal.json          -> 0   optimal, x=[2,6], objective=-36
02_equality_phase1.json  -> 0   optimal, x=[0,2,8], objective=14, Phase I 3 步
03_infeasible.json       -> 0   infeasible, farkas_y=[-1,1], y^T b=2
04_unbounded.json        -> 0   unbounded, ray=[1,0], 目标方向=-1
05_bounded_vars.json     -> 0   optimal, x=[2,3], objective=22（max，变量上界）
06_constraint_list.json  -> 0   optimal, x=[5,0], objective=10（约束列表式）
```

完整响应见 `examples/responses/*.json`，其中所有 `max_abs_residual` 为 0
（浮点绝对量级 ≤ 1e-12）。

非法 JSON：

```
$ echo '{not json' | python3 -m bounded_lp.cli
{ "status": "invalid_request",
  "errors": ["JSON 语法错误：Expecting property name enclosed in double quotes（行 1 列 2）"] }
退出码 2
```

## 4. 循环风险：Beale 问题实测

经典 Beale 例子（`min -3/4 x1 + 150 x2 - 1/50 x3 + 6 x4` 及三个给定 BFS 等式）：

```
$ python3 scripts/beale_demo.py   # 等价内联脚本
Bland   : optimal obj=-0.0500 p1=5 p2=1
Dantzig : optimal        None    p1=3 p2=2   （浮点下被舍入“解救”，未精确复现循环）
枚举参考 : optimal obj=-0.0500
```

* Bland 规则终止于最优值 **−0.05 = −1/20**，与顶点枚举参考一致；
* 浮点 Dantzig 在本例中因舍入误差打破了精确循环，3+2 步后到最优——
  这是文献中已知现象，**不能**据此声称浮点下不会循环。因此测试
  `test_beale_cycles_under_exact_arithmetic` 用 `fractions.Fraction`
  精确表上作业，确定性地断言 **6 次枢轴内基组合回到起点**，作为循环存在性的
  可靠证据；浮点 Dantzig 用例只断言“在迭代上限内终止且不返回错误结论”
  （optimal 或 failed），不依赖具体分支，避免脆弱测试。

## 5. 中规模性能抽查（稠密 Python/NumPy，教学定位）

构造可行域有界、且含已知可行点的随机整数/正态 LP（含变量上界行）：

```
n= 30 rows= 60  it= 203   time=   40.4 ms  resid=5.24e-14
n= 50 rows=100  it= 645   time=  225.5 ms  resid=1.26e-13
n=100 rows=200  it=2465   time= 1727.0 ms  resid=6.68e-13
n=150 rows=250  it=7884   time= 7116.0 ms  resid=3.24e-12
all bounded & correct
```

残差均 ≤ 1e-11；目标值不劣于已知可行点。150×250 约 7 秒，符合小中规模
教学实现预期，不作为高性能求解器宣传。

## 6. 开发过程中发现并修复的问题（如实记录）

1. `np.nditer` 迭代零大小数组报错 → 改为普通索引循环。
2. 一处生成器表达式变量名笔误（`k`/`kinds[i]`）→ 修正。
3. **检验数行符号约定不一致**：初版表行存的是 z_j−c_j 却按 c_j−z_j 的
   规则选进入列，导致 `min -3x1-5x2` 误判原点最优。统一约定
   “行 0 = c_j − y^T A_j，RHS = −z”，同步修正 Phase I/II 行构造、
   最优值读取与 Farkas 读出。
4. **Farkas 证书两次读错**：先在“松弛基行取 0”和“终基列”之间混淆，
   得到全零证书。正确读法是初始基列的当前表列（B^{-1} 各列）：
   `y_i = c_p1[初始基列_i] − T[0,初始基列_i]`，并以 `y^T A≤0, y^T b>0`
   为约定；增加“非负约束本身导致不可行”的随机证书测试锁死该情形。
5. 参考枚举初版只枚举活跃约束零空间的基向量 ±方向，漏掉“零空间内部组合
   可行”的衰退射线（seed=20 把真无界误判有界）。改为枚举衰退锥的
   **极射线**（取 n−1 个活跃面法向联立），极射线非负组合若严格改善则必有
   一条极射线严格改善，判定完备；同时修了无约束行时的空矩阵乘法。
6. 无界响应残差缺 `max_abs_residual` 键导致 KeyError → 补齐射线残差聚合。
7. 残差字段 `lb_violation` 曾为负数（语义是裕量不是违反量），易误读 →
   拆成 `ub_max_slack_or_violation`（带符号裕量）与一律非负的
   `*_violation` 字段。
8. 两次编辑把相邻代码行粘连导致 SyntaxError；`json.loads` 的
   parse_constant 抛 `ValueError` 未捕获导致 NaN 请求 500 式失败 →
   统一转为 `invalid_request`。
9. 测试自身用了正则转义串做普通子串匹配，报了两个假阳性失败 → 修测试。

## 7. 未覆盖/已知限制

* 未实现稀疏矩阵、对偶单纯形、内点法；规模与性能按 README 声明受限。
* 容差为绝对容差，未做问题自动缩放。
* 仅非负（含有限非负下界）连续变量；不支持自由变量、整数变量。
