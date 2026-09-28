# Exact Rational Matrix Service

精确有理线性方程求解与秩服务。所有计算在 **Python 任意精度整数 / `fractions.Fraction`** 上完成，
使用**无分数 Bareiss 消元（fraction-free elimination）**配合行内容约分与位数预算；
**任何路径都不会静默退化为浮点**。NumPy/SciPy/mpmath 只用于结果中明确标注为
"approximate" 的对照诊断。

## 能力

- 精确秩、零化度、主元/自由列、行交换记录、（方阵满秩时的）精确行列式；
- 三类右端项分别判定：**唯一解 / 无穷多解（参数化形式）/ 矛盾（无解）**；
- 无穷多解返回 `x = particular + Σ t_k · nullspace_basis[k]`，可独立代回；
- 矛盾系统返回**左零空间见证向量 y**（原始方程坐标），满足 `yᵀA = 0` 而 `yᵀb ≠ 0`，
  该证据由独立代码路径重新核验；
- 整数系数增长有**位数预算**，耗尽即返回 `budget_exhausted`（HTTP 422）并携带
  可诊断、可重放的中间矩阵快照、主元与交换记录；
- 每次运行有 `run_id`，JSONL 运行日志保留精确输入与每个关键步骤，支持 HTTP/CLI 重放；
- 大公因子矩阵先做行内容约分，控制系数增长；行交换与约分不改变符号。

## 目录结构

```
src/rationalsvc/
  errors.py        错误分类（闭合枚举）与异常 —— 跨模块错误契约
  frac.py          精确数边界：JSON 标量 -> Fraction（拒绝 float）
  numeric_input.py 请求载荷解析与形状/预算校验
  kernel.py        无分数 Bareiss 消元内核（仅整数，精确除断言，见证矩阵，预算钩子）
  evidence.py      解提取、独立代回核验、左零空间见证、float64/mpmath 对照诊断
  runner.py        编排：清分母、预算、run_id、JSONL 日志、结果封装
  api.py           FastAPI 接口（/solve /rank /health /runs/{id}）
  replay.py        命令行重放工具
tests/
  oracle.py        独立参考实现（朴素 Fraction Gauss-Jordan + mpmath + numpy）
  test_*.py        单元 + 集成测试（断言具体数值与失败类别）
data/fixtures.json 本地合成夹具（含手工推导的期望值）
run.sh             本地启动脚本
```

模块间数据契约见 [`docs/architecture.md`](docs/architecture.md)。

## 快速开始

需要 Python 3.10+。依赖：FastAPI、uvicorn、pydantic、NumPy、SciPy、mpmath（测试用 pytest、httpx）。

```bash
pip install -r requirements.txt        # 或 pip install -e '.[test]'
./run.sh                               # 默认 127.0.0.1:8000，日志写 run_log.jsonl
```

请求里只接受**精确标量**：JSON 整数、十进制/指数字符串（`"-0.25"`、`"1e3"`）、
比例字符串（`"2/3"`）。**JSON 浮点数一律拒绝**（错误类别 `input_precision_unsupported`）。

```bash
curl -s -X POST http://127.0.0.1:8000/solve -H 'content-type: application/json' -d '{
  "A": [[1, 2, 3],
        [4, 5, 6],
        [7, 8, "9000000000000001/1000000000000000"]],
  "b": [6, 15, "24000000000000001/1000000000000000"],
  "digit_budget": 200
}'
```

该矩阵在 float64 下与奇异矩阵**不可辨别**（`numpy.linalg.matrix_rank` 给出 2），
而服务给出精确结论（实测输出）：

```
rank exact/float64: 3 / 2
classification: unique
x: ['1', '1', '1']
determinant: -3/1000000000000000
```

### 错误类别（可区分，而非一个笼统 500）

| 类别 `error`              | HTTP | 含义                          |
|---------------------------|------|-------------------------------|
| `input_malformed`         | 400  | 体非 JSON / 字段缺失 / 类型错 |
| `input_empty`             | 400  | 空矩阵                        |
| `input_shape_mismatch`    | 400  | 行数不齐、b 维度不符          |
| `input_not_representable` | 400  | 标量无法表示为精确有理数      |
| `input_precision_unsupported` | 400 | 发送了 float（拒绝精度降级） |
| `state_conflict`          | 409  | 预算非正/超上限、want 非法    |
| `budget_exhausted`        | 422  | 位数预算耗尽，携带中间进度    |
| `computation_failed`      | 500  | 内部不变量失败（含诊断）      |

### 重放

```bash
# HTTP
curl http://127.0.0.1:8000/runs/<run_id>
# CLI（可不加 --digit-budget 用原预算重算，或给更小预算复现耗尽）
PYTHONPATH=src python -m rationalsvc.replay --last
PYTHONPATH=src python -m rationalsvc.replay --list
PYTHONPATH=src python -m rationalsvc.replay <run_id> --digit-budget 1
```

## 运行测试

```bash
PYTHONPATH=src python -m pytest -q --cov=rationalsvc --cov-report=term
```

实测结论（Python 3.12.3）：

```
124 passed, 1 warning in 3.52s

Name                               Stmts   Miss  Cover
src/rationalsvc/__init__.py            1      0   100%
src/rationalsvc/api.py                73      2    97%
src/rationalsvc/errors.py             44      0   100%
src/rationalsvc/evidence.py          152      3    98%
src/rationalsvc/frac.py               33      4    88%
src/rationalsvc/kernel.py            194      5    97%
src/rationalsvc/numeric_input.py      77      7    91%
src/rationalsvc/replay.py             76      5    93%
src/rationalsvc/runner.py            100      2    98%
TOTAL                                750     28    96%
```

测试不是"接口能调用"式检查：

- 断言具体解、行列式、自由列、见证向量的**精确字符串值**（见 `data/fixtures.json` 的 `expected`）；
- `tests/oracle.py` 是与生产内核**不同实现**的独立 oracle（朴素 Fraction Gauss-Jordan、
  独立左零空间推导、mpmath 80 位行列式），`test_kernel.py` 对数十个随机系统差分对拍；
- 每个错误类别都有对应 HTTP 状态码与 `error` 字段断言；
- 预算耗尽断言中间快照可重放且所有条目仍是整数字面量（证明无浮点回退）。
