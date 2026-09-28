# 有理系数多项式实根隔离服务

基于 **Sturm 序列**的精确实根隔离服务。输入为有理系数多项式，输出：

- 一组**两两互不相交的有理区间**（开区间或精确有理单点），每个区间恰含一个不同实根；
- 每个区间的**根数证明**（Sturm 符号变化数 `V(lo) − V(hi)`）；
- 每个根的**重数**（由精确无平方因子分解得到，重根与不同根数严格区分）；
- 每个区间的**独立误差证据**（mpmath 有向舍入区间算术 + SciPy `brentq` + NumPy 伴侣矩阵特征值三种相互独立的见证）；
- 明确的 **接受 / 拒绝 / 无法判定（accepted / rejected / undetermined）** 分类与带请求标识的诊断信息。

所有根计数判定走 `fractions.Fraction` **精确有理算术**，不存在浮点误判；浮点库只承担独立见证角色。

---

## 1. 目录结构

```
app/
  polynomial.py    精确有理多项式算术、gcd、Sturm 链、精确符号
  squarefree.py    Musser 无平方因子分解（区分重数）
  kernel.py        Sturm 二分隔离、端点根处理、互斥后置处理、预算计数
  numeric_io.py    外部系数精确解析（整数/小数/'p/q'/科学计数法）与边界预算
  evidence.py      mpmath 区间 / SciPy brentq / NumPy 特征值独立证据
  service.py       应用编排：解析 → 隔离 → 证据；零多项式特例；三态分类
  schemas.py       FastAPI 请求/响应模型
  api.py / main.py 薄路由、应用工厂、请求标识与脱敏中间件
  settings.py      配置加载与预算校验
config/default.yaml  预算与服务配置（单一事实来源）
scripts/           run_server.py（启动）/ isolate_cli.py（命令行客户端）
tests/
  oracles.py       独立参考答案（整数伪余数 Sturm + mpmath.polyroots）
  fixtures/        本地合成夹具 JSON
  test_*.py        单元 / 内核 / 证据 / 服务 / HTTP 集成测试
```

模块职责真实分离：**数值输入 / 计算内核 / 误差证据 / 服务接口** 四层，测试与配置独立组织。

---

## 2. 安装（锁定依赖）

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

关键依赖已锁定（Python 3.12 上验证）：

| 包 | 版本 | 角色 |
|---|---|---|
| fastapi / uvicorn / starlette / pydantic | 0.141.1 / 0.54.0 / 1.7.0 / 2.13.5 | HTTP 接口 |
| numpy | 2.5.3 | 伴侣矩阵特征值独立见证 |
| scipy | 1.18.1 | `brentq` 变号独立见证 |
| mpmath | 1.4.1 | 有向舍入区间算术与高精度预言机 |
| PyYAML | 6.0.3 | 配置 |
| pytest / pytest-cov / httpx | 8.4.2 / 5.0.0 / 0.28.1 | 测试 |

---

## 3. 启动服务

```bash
.venv/bin/python -m scripts.run_server
# 或 .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
```

交互式 API 文档：`http://127.0.0.1:8000/docs`。健康检查：`GET /health`。

---

## 4. 示例调用

### 4.1 普通三次多项式 `(x−1)(x−2)(x−3) = x³−6x²+11x−6`

系数按**升幂**给出（索引 `i` 乘 `x**i`）：

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/isolate-real-roots \
  -H "Content-Type: application/json" \
  -d '{"coefficients":[-6,11,-6,1],"target_width":"1/1000000"}'
```

每个根返回一个有理开区间与证明，例如：

```json
{
  "lo": "4194303/4194304",
  "hi": "2097153/2097152",
  "exact": false,
  "multiplicity": 1,
  "proof": {
    "method": "sturm",
    "variations_left": 3,
    "variations_right": 2,
    "root_count_in_cell": 1,
    "chain_length": 4,
    "open_interval": true,
    "convention": "V(lo) - V(hi) counts roots in (lo, hi]"
  }
}
```

### 4.2 重根 `(x−1)²(x−2)(x−3)³`

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/isolate-real-roots \
  -H "Content-Type: application/json" \
  -d '{"coefficients":[54,-189,261,-182,68,-13,1]}'
```

返回 3 个**不同实根**，重数分别为 `2`（开区间）、`1`（精确单点 `2`）、`3`（开区间）。
`multiplicities` 字段给出每个无平方因子的次数、Sturm 链长、Cauchy 整数界与根数。

### 4.3 区间端点恰为根（重点约束）

搜索区间按**闭区间 `[lo, hi]`** 语义处理：端点恰为根时作为精确单点返回，且只返回一次。

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/isolate-real-roots \
  -d '{"coefficients":[54,-189,261,-182,68,-13,1],
       "interval_lo":"1","interval_hi":"3"}'
# 精确根 1(m=2)、2(m=1)、3(m=3)，无重复计数
```

内部二分使用半开区间 `(a,b]` 约定（根在右端计入、左端不计入），左闭端点在进入时单独处理，因此闭区间语义与精确计数同时成立。

### 4.4 近邻根 `x² − 10⁻¹²`（根在 ±10⁻⁶）

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/isolate-real-roots \
  -d '{"coefficients":["-1/1000000000000",0,1],
       "target_width":"1/100000000000000"}'
```

两个根被严格分开（一个区间整体在 0 左、一个整体在 0 右）。

### 4.5 高动态范围系数 `10²⁰x² − 10⁻²⁰`

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/isolate-real-roots \
  -d '{"coefficients":["-1/100000000000000000000",0,100000000000000000000]}'
```

系数跨 40 个数量级仍精确隔离 ±10⁻²⁰；此时 float64 特征值见证被明确标记为
`reliable=false`（**无法判定**而非覆盖精确结果），mpmath 有向舍入与 brentq 见证仍成立。

### 4.6 常数零多项式（特例，不套普通根列表）

```bash
curl -s -X POST .../isolate-real-roots -d '{"coefficients":[0,0,0]}'
```

```json
{"status":"zero_polynomial","is_zero_polynomial":true,"roots":[],
 "diagnostics":{"decision":"accepted",
   "reason":"all coefficients are zero; every point is a root, so no finite isolating interval list exists"}}
```

非零常数返回 `status:"ok", degree:0, roots:[]`。

### 4.7 命令行客户端

```bash
.venv/bin/python -m scripts.isolate_cli --coeffs '[-6,11,-6,1]'
.venv/bin/python -m scripts.isolate_cli --file tests/fixtures/repeated_roots.json
.venv/bin/python -m scripts.isolate_cli --coeffs '[1,0,1]' --width 1/1000
```

系数支持 JSON 整数、JSON 浮点（按其十进制 `repr` 精确还原，**不**走二进制近似）、
`"p/q"` 分数字符串与 `"1.25e-3"` 科学计数法。

---

## 5. 精确性、预算与三态结论

### 精确算术

- 系数与所有二分点均为 `Fraction`；Sturm 链、gcd、无平方因子分解、端点符号全部精确。
- 初始包围界用 **Cauchy 整数界**（`⌊M⌋+2`，`M=max|aᵢ/aₙ|`，在精确 Fraction 上取整），保证初始端点不是根。
- 二分中若中点恰为有理根，立即作为**精确单点**输出，不会无限细化。

### 显式预算（`config/default.yaml`）

| 预算 | 默认 | 超限行为 |
|---|---|---|
| `max_degree` | 200 | `DEGREE_EXCEEDED`（rejected） |
| `max_coefficients` | 201 | `TOO_MANY_COEFFICIENTS`（rejected） |
| `max_coeff_bits` | 20000 | `COEFFICIENT_TOO_LARGE`（rejected） |
| `max_bisections` | 20000 | `BISECTION_BUDGET_EXCEEDED`（**undetermined**，返回已用量等关键状态） |
| `max_sturm_length` | 202 | `STURM_LENGTH_EXCEEDED`（undetermined） |
| `precision_dps` | 100 | 证据起点；证据模块按需倍增，封顶 `evidence.MAX_EVIDENCE_DPS=4000` |
| `http.max_body_bytes` | 65536 | 413 `PAYLOAD_TOO_LARGE` |

证据模块的有向舍入精度对窄区间**自适应倍增**：端点若非根，提高精度必能得到严格单侧区间；
到封顶仍不能判定则该单元为 `inconclusive`，整体 `undetermined`，绝不猜测。

### 结论分类

- `accepted`：精确隔离完成且独立见证全部通过；
- `rejected`：请求本身非法（具体失败码 + 关键状态）；
- `undetermined`：输入合法但预算/精度耗尽，或独立见证无法确认（附原因与已用预算）。

HTTP 对“格式正确但语义非法”返回 **200 + 体内 `status:"rejected"`**；
JSON/schema 结构性错误返回 **422**；请求体超限返回 **413**。

---

## 6. 诊断与脱敏

- 每个请求带 `request_id`（客户端可用 `X-Request-ID` 头指定），同时出现在响应头、响应体和访问日志。
- 日志只记录方法、路径、状态、耗时与请求标识，**不记录系数值**。
- 422 错误处理器剥离 Pydantic 的 `input` 字段；非法分数字符串只回显形状（如 `string(length=10)`）。
- 诊断状态只保留小型标量事实（次数、位宽、已用二分次数等），其余以类型占位符脱敏。

---

## 7. 独立测试（参考答案不来自被测内核）

```bash
.venv/bin/python -m pytest
```

`tests/oracles.py` 提供两条与内核实现完全不同的参考路径：

1. **整数伪余数 Sturm 链**（自含的整数多项式例程，含 `lc(b)` 负数时的符号校正），
   独立复算符号变化数与根数；
2. **`mpmath.polyroots` 高精度全局求根**，独立核对根的位置与个数（重根情形在 radical 上核对）。

> 开发中，独立预言机与被测 Fraction 链在 `(x²+1)(x⁴+1)` 上结果不一致，
> 据此发现并修正了预言机伪余数在负首项系数下的符号错误——这正是独立交叉验证的价值。

测试断言**具体结果与失败类别**，而非“接口能调用”：手算符号表、精确重数、近邻根严格分离、
无实根数、高动态范围、闭区间端点根、互斥性、预算耗尽失败码、脱敏、413/422 等。

覆盖验证案例：

| 夹具 | 多项式 | 断言要点 |
|---|---|---|
| `repeated_roots.json` | `(x−1)²(x−2)(x−3)³` | 不同根数=3，重数 2/1/3 |
| `near_roots.json` | `x²−10⁻¹²` | ±10⁻⁶ 两根严格分开 |
| `no_real_roots.json` | `(x²+1)(x⁴+1)` | 0 实根，Sturm 变化数相等 |
| `high_dynamic_range.json` | `10²⁰x²−10⁻²⁰` | 40 数量级跨度仍精确 |
| `endpoint_roots.json` | 区间 `[1,3]` 两端皆根 | 精确单点且不重复 |

当前结果：**103 passed，行覆盖率约 94%**。

---

## 8. 已实际执行的验证

- 完整测试套件（精确算术 / Sturm / 无平方因子 / 内核 / 证据 / 服务 / HTTP）通过；
- 真实启动 uvicorn，用 `curl` 验证了普通根、重根、零多项式、近邻根、无实根、
  端点根、422、脱敏等端到端路径；
- 宽度 `10⁻¹⁵⁰` 的请求由证据模块自适应升到 200 dps 后接受（1353 次二分）；
- 宽度 `10⁻⁷⁰⁰⁰` 的请求在 20000 次二分后明确返回 `BISECTION_BUDGET_EXCEEDED`。

复现：

```bash
.venv/bin/python -m pytest                       # 全部测试 + 覆盖率
.venv/bin/python -m scripts.run_server           # 启动服务
```

---

## 9. 剩余限制（明确说明）

1. **实根隔离、非复根**：只隔离实根；复数根不输出（但无实根时由 Sturm 严格证明）。
2. **精确算术的计算成本**：高次数 + 极窄宽度会让二分分母位数线性增长；虽有 20000 次预算兜底，
   极端请求会返回 `undetermined` 而非强行给结果。
3. **float 见证的适用域**：NumPy 伴侣矩阵特征值在系数动态范围 > 10¹² 或次数 > 60 时被标记
   `reliable=false`；此时精确 Sturm 结果仍成立，但该路独立见证不计入通过条件。
4. **零多项式语义**：零多项式处处为根，无法给出有限的隔离区间列表，因此返回专门的
   `zero_polynomial` 状态，而不是空根列表伪装成成功。
5. **隔离而非代数求根**：输出是“恰含一个根的有理区间”，不是根式/代数表达式；有理根在被二分
   精确命中时才作为精确单点给出。
6. **本地服务**：面向本地合成数据，无鉴权/多租户；请求体大小与系数预算是唯一的输入闸门。
