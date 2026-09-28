# Exact Rational Linear Equations & Rank Service

精确有理线性方程求解与秩判定服务。所有计算都在有理数上进行（Python
`fractions.Fraction` + **无分数 Bareiss 消元**），整数系数增长受**位数预算**
约束，超预算时返回可诊断的中间进度而**绝不静默退化为浮点**。

技术栈：Python 3.10+、FastAPI、Pydantic v2、NumPy、SciPy、mpmath、pytest。

---

## 它解决什么问题

二进制浮点对三类矩阵会给出误导性结果，本服务用精确算术给出可核验答案：

| 情形 | 浮点的问题 | 本服务 |
|------|-----------|--------|
| 大公因子矩阵 | 中间数巨大、可能丢精度 | 精确解，结果约分至最小 |
| 秩亏 | 依赖容差，可能误判满秩 | 精确秩 + 精确零空间基 |
| 近浮点不可辨别 | 条目被舍入成相同值，误判秩 | 精确秩（示例：精确秩 2，float64 判 1） |

`data/fixtures/near_float_indistinguishable.json` 中
`[[10^16+1, 10^16], [10^16, 10^16-1]]` 的精确行列式为 **-1**、精确解 `(1,1)`，
但四个元素在 float64 下全部变成 `1e16`，NumPy 默认容差报秩为 1。

---

## 快速开始

```bash
# 1) 安装依赖（建议虚拟环境）
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[test]"        # 或: pip install -r requirements.txt

# 2) 运行测试（真实命令，结论见下）
python3 -m pytest -q

# 3) 启动服务
./scripts/run.sh                # 默认 http://127.0.0.1:8000
# 或: PYTHONPATH=src python3 -m uvicorn rational_linalg.service.app:app

# 4) 不用服务也能看核心行为
PYTHONPATH=src python3 examples/demo_core.py
```

交互式 API 文档：启动后访问 `http://127.0.0.1:8000/docs`。

### 真实测试命令与结论

```
$ python3 -m pytest -q
.........................................                              [100%]
49 passed, 1 warning in 1.36s
```

覆盖率（`python3 -m pytest --cov=rational_linalg --cov-report=term-missing`）：

```
Name                                      Stmts   Miss  Cover
-----------------------------------------------------------------------
budget.py                                    62      1    98%
errors.py                                    39      1    97%
evidence.py                                  58      2    97%
floatdiag.py                                 32      4    88%
kernel.py                                    89      1    99%
numbers.py                                   60      1    98%
run_log.py                                   59      0   100%
service/app.py                               65      3    95%
service/engine.py                            74      1    99%
service/schemas.py                           18      0   100%
solve.py                                     81      3    96%
-----------------------------------------------------------------------
TOTAL                                        638     17    97%
```

### `examples/demo_core.py` 真实输出（节选）

```
=== unique, big common factor 10^20 ===
classification: unique | rank A: 2 | rank [A|b]: 2
particular x0  : ['1', '2']
substitute t = 3/2 -> exact residual zero: True

=== infinitely many solutions ===
classification: infinite | rank A: 2 | rank [A|b]: 2
particular x0  : ['0', '3', '0']
null direction 0: ['1', '-2', '1']

=== inconsistent ===
classification: inconsistent | rank A: 1 | rank [A|b]: 2
witness y      : ['-1', '1']
y^T A (zeros)  : ['0', '0']
y^T b (nonzero): 1
```

---

## HTTP 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET  | `/health` | 存活与算术模式 |
| POST | `/api/v1/solve` | 精确求解 `A x = b` |
| POST | `/api/v1/rank` | 精确秩 + 左零空间 |
| GET  | `/api/v1/runs/{run_id}` | 重放某次运行的事件/结果 |
| GET  | `/api/v1/error-codes` | 全部可区分错误码目录 |

**输入只接受 JSON 整数或精确字符串**（`"3"`、`"3/2"`、`"0.1"`、`"1.5e3"`）。
JSON 小数会变成 Python `float`，被显式拒绝（`NON_EXACT_NUMBER`），
从协议层杜绝静默浮点。

### 三种分类（Rouché–Capelli 精确定理）

- `unique`：`rank(A)=rank(A|b)=n`，返回唯一解；
- `infinite`：`rank(A)=rank(A|b)<n`，返回特解 `particular` + 零空间基，
  参数形式 `x = particular + Σ t_j·basis[j]`，可取任意有理参数独立代回；
- `inconsistent`：`rank(A)<rank(A|b)`，返回左零空间矛盾证据 `y`，
  满足 `y^T A = 0` 而 `y^T b ≠ 0`，可独立证明无解。

### 示例请求

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/solve \
  -H 'Content-Type: application/json' \
  -d '{"A":[[1,2,3],[4,5,6],[6,9,12]],"b":[6,15,27]}'
# classification=infinite, free_columns=[2], null direction (1,-2,1)
```

更多例子见 `scripts/example_requests.sh`。

### 可区分的失败类别

| category | HTTP | 典型 code | 含义 |
|----------|------|-----------|------|
| `INPUT_ERROR` | 400 | `NON_EXACT_NUMBER`, `RAGGED_MATRIX`, `DIMENSION_MISMATCH`, `SCHEMA_VALIDATION_FAILED` | 调用方数据问题 |
| `STATE_CONFLICT` | 409 | `RUN_NOT_FOUND`, `RUN_ID_CONFLICT` | 运行状态冲突 |
| `RESOURCE_EXHAUSTED` | 507 | `DIGIT_BUDGET_EXCEEDED`, `STEP_BUDGET_EXCEEDED` | 预算耗尽，**附中间进度** |
| `COMPUTATION_FAILED` | 500 | `BAD_PIVOT` | 内部不变量被破坏 |

错误统一信封：`{"error": {"category", "code", "message", "details", "progress"?}}`。

预算耗尽响应中的 `progress` 包含：已完成主元数、主元列、行交换记录、
部分矩阵快照、位数轨迹（每个主元观测到的最大位数），足以定位与重放。

---

## 工程结构

```
src/rational_linalg/
  errors.py          四类可区分错误 + 预算失败的进度载体
  numbers.py         精确入参解析（拒绝 float/NaN/Inf），矩阵形状校验
  budget.py          十进制位预算与步数预算、快照与轨迹
  kernel.py          Bareiss 无分数消元、行交换、左乘变换 T 追踪、符号契约
  solve.py           精确 RREF、唯一/无穷/矛盾分类、秩与左零空间
  evidence.py        独立精确代回核验、矛盾证据、行列式、mpmath 高精度呈现
  floatdiag.py       NumPy/SciPy 浮点对照（明确标注为有损，永不参与结论）
  run_log.py         run_id、事件收集、JSONL 持久化、运行注册表
  service/
    schemas.py       Pydantic 请求契约
    engine.py        解析→精确计算→独立证据 的编排与数据契约
    app.py          FastAPI 路由、错误信封、运行重放
tests/
  independent_oracle.py  独立朴素 Fraction Gauss-Jordan 预言机（非被测代码）
  test_*.py             单元 + HTTP 集成测试
data/fixtures/          合成夹具（含手工/独立推导的 expected 答案）
examples/demo_core.py   库级演示
scripts/                run.sh、example_requests.sh
```

### 模块间契约

- **输入边界** (`numbers`)：唯一产出类型是 `list[list[Fraction]]`；非法输入
  只能抛 `InputError`。
- **计算内核** (`kernel`)：输入输出均为 `Fraction`，不捕获/不转浮点；
  通过 `DigitBudget` 在每次主元更新后检查位数，超预算抛
  `ResourceExhaustedError` 并附带快照。
- **分类层** (`solve`)：返回结构化 dict（分类、秩、主元、解/证据、
  `EliminationResult`、预算账本），不接触 HTTP。
- **证据层** (`evidence`)：只依赖原始输入做独立核验，不信任求解器自身记录。
- **服务层** (`engine`/`app`)：负责 Fraction↔JSON 序列化、错误信封、日志。

### 符号契约

行交换与 Bareiss 精确除法是仅有的行操作。**绝不对行乘 -1 来“美化”符号**；
`Fraction` 约分只除去正的 gcd，不改变符号。因此主元、行列式符号、解分量
符号都精确保留（`tests/test_budget_kernel.py` 中对负主元、反对角阵有断言）。

### 失败重放

每次运行写 `logs/runs.jsonl`：每个消元事件一行（主元选取、行交换、位数
观测），末尾一行 `run_summary`。预算失败时保留 `run_id`、关键中间状态和
判断理由，仅凭该文件即可重放。测试
`test_budget_failure_log_is_replayable_with_intermediate_state` 对此有断言。

---

## 设计要点

- **Bareiss 无分数消元**：`M'[i,j] = (M[i,j]·pivot − M[i,col]·pivot_row[j]) / prev_pivot`
  是精确整除恒等式，商恒为整数，中间数即矩阵的主子式，增长天然受控、可预算。
- **变换矩阵追踪**：同步维护 `T`，始终有 `T·M_original = M_current`；
  最终零行对应的 `T` 行就是精确左零空间向量，矛盾证据由此直接得到。
- **独立预言机**：`tests/independent_oracle.py` 用与生产内核不同的朴素
  Gauss-Jordan 实现，随机矩阵交叉核验秩/解/行列式；夹具的 `expected`
  为手工或独立推导，**参考答案不由被测核心生成**。
- **mpmath 仅用于展示**：把精确分数渲染成高精度十进制字符串，不参与任何
  正确性判定。

## 配置

环境变量（见 `.env.example`）：

- `RATIONAL_LINALG_DIGIT_BUDGET`：整数系数最大十进制位数（默认 4096，可按请求覆盖）；
- `RATIONAL_LINALG_DECIMAL_DPS`：展示用十进制精度（默认 40）；
- `RATIONAL_LINALG_LOG_DIR`：JSONL 日志目录（默认 `logs`）。

## 局限

- 纯 Python 大整数，面向中小规模教学/验证场景；超大矩阵的性能不是目标。
- 运行注册表是进程内的，`GET /runs/{id}` 只在同一进程可见；JSONL 日志则跨进程持久。
