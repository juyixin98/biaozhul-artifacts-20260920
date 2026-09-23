# 集中流动性区间报价引擎（Concentrated Liquidity Quote Engine）

离线、纯后端的**集中流动性（Uniswap V3 风格）精确输入报价引擎**。
使用 **Python 3.12 + FastAPI + SQLite**，所有货币与价格计算均为**整数定点运算**，
热点路径不使用任何浮点数；每条报价绑定**不可变池快照**，并附带逐段价格、流动性、
费用证据，以及用 `fractions.Fraction` + `decimal.Decimal` 慢速独立参考实现做的交叉验证。

---

## 1. 数值约定（务必先读）

### 整数 tick 宇宙

* Tick 为**有符号整数**，闭区间 `[-100_000, 100_000]`（见 `app/config.py`）。
* Tick 底数 `b = 10001/10000 = 1.0001`（与 Uniswap V3 一致）。
* 人类可读价格：`price(i) = 1.0001 ** i`；平方根价格 `sqrt_price(i) = 1.0001 ** (i/2)`。

### 项目自定义定点精度

* 平方根价格在引擎内部是一个**整数定点数**，隐含 `38` 位小数：
  `SP(i) = floor(10**38 * 1.0001**(i/2))`（`SQRT_SCALE = 10**38`）。
* 精确实现（`app/tick.py`）：`SP(i) = isqrt(floor(SQRT_SCALE² · num/den))`，
  其中 `num/den = (10001/10000)^i`。利用恒等式
  `isqrt(floor(x)) == floor(√x)`，结果是数学意义上的**精确 floor**，没有近似误差。
* 代币数量一律是**最小单位（wei）的非负整数**，金额大数在 JSON 中以**字符串**传输
  （同时接受整数输入）。

### 舍入方向（对兑换者保守、对池有利）

| 量 | 舍入 | 说明 |
|---|---|---|
| tick → 平方根价格 | **floor** | 定点规范价格 |
| 跨界所需净输入 | **ceil** | 不允许"没付够却跨界" |
| 每段实际净输入 `curve` | **floor** | 不多收 |
| 每段产出 `out` | **floor** | 绝不多付 |
| 协议费 `fee` | **ceil** | 绝不少收、不跳过成本 |
| 最后一段的终止 sqrt 价 | 池有利方向 | 二分搜索"最大可承受移动" |

费用按每段**真正进入曲线的净输入**收取：

```
fee(c)   = ceil(c · F / (D − F))
gross    = c + fee(c) = ceil(c · D / (D − F))
```

默认费率 `F/D = 3000/1_000_000 = 0.30%`。当 `L == 0`（空区间）或 `Sa == Sb`
（零价格步长）时**不做任何除法**，从结构上杜绝除零。

### AMM 恒等式（单区间内，`S = SQRT_SCALE`）

```
zero → one（价格下行，token0 进、token1 出）
    dx(净输入) = L·S·(Sa − Sb)/(Sa·Sb)
    dy(产出)   = L·(Sa − Sb)/S

one → zero（价格上行，token1 进、token0 出）
    dy(净输入) = L·(Sb − Sa)/S
    dx(产出)   = L·S·(Sb − Sa)/(Sa·Sb)
```

---

## 2. 交换与停止规则

交换可顺序穿越多个流动性区间，**逐段扣费、逐段记账**。终止原因：

* `input_exhausted`：全部输入在某区间内被消耗（正常成交完毕）。
* `input_dust`：剩余输入小到连最小的非零整数步都付不起，剩余输入原样返回。
* `empty_range`：下一个区间 `L == 0`，**立即停止并返回全部未成交输入**。
* `tick_boundary`：已走到 tick 宇宙边缘 `TICK_MIN/TICK_MAX`。

每条返回都包含：

* `unspent_input`：未成交并返回的输入；
* 逐段 `Segment` 证据：区间 tick、流动性、起止 sqrt 价、毛输入/净输入/费用/产出、
  是否跨界、文字说明；
* 守恒恒等式：`Σ gross_input + unspent_input == amount_in`；
* `pool_digest` / `quote_digest`：真实 **SHA-256**（标准库 `hashlib`，OpenSSL 后端）
  对快照与全部证据的绑定摘要，防篡改。

**报价绝不修改储备/快照**：池对象不可变（frozen dataclass），报价只读快照。

---

## 3. 目录结构

```
app/
  config.py     # tick 宇宙、1.0001 底数、10**38 定点、费率
  tick.py       # 整数 tick <-> 定点 sqrt 价（精确 floor）+ Decimal 高精度展示
  models.py     # 不可变 Pool / Position / Segment / QuoteResult；区间流动性表
  engine.py     # 热点报价引擎：跨区间逐段、费用、停止规则（纯整数 + 二分）
  reference.py  # Fraction/Decimal 慢速独立参考实现（验证用，热点路径不调用）
  crypto.py     # 池快照与报价的 SHA-256 摘要绑定与校验
  db.py         # SQLite 持久化（只写不改：pools / quotes）
  schemas.py    # Pydantic 请求模型（大数接受 int 或字符串）
  main.py       # FastAPI 路由
examples/       # 示例请求 JSON + 可运行 demo
tests/          # pytest：tick 映射 / 引擎 / 边缘 / 摘要 / HTTP 共 59 个用例
requirements.txt# 锁定依赖
```

---

## 4. 本地启动

```bash
cd /home/admin/Downloads/biaozhul/P012/a
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

# 初始化并启动（数据库默认 data/clm.db，可用 CLM_DB_PATH 覆盖）
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

启动后：

* 健康检查：`GET http://127.0.0.1:8000/health`
* Swagger 交互文档：`http://127.0.0.1:8000/docs`
* OpenAPI：`http://127.0.0.1:8000/openapi.json`

### 命令行快速验证（无需起服务）

```bash
. .venv/bin/activate
python -m examples.demo
```

该脚本构造三个活跃区间（上方留空），在两个方向上穿越两个 tick 边界，
打印逐段证据并对每笔报价执行 Fraction/Decimal 独立参考校验（输出 `MATCH`）。

---

## 5. HTTP 用法示例

```bash
# 1) 创建不可变池快照
curl -s -X POST http://127.0.0.1:8000/pools \
  -H 'Content-Type: application/json' \
  -d @examples/create_pool.json

# 2) 报价：zero -> one（金额可用字符串或整数）
curl -s -X POST http://127.0.0.1:8000/pools/weth-usdc-30bp/quote \
  -H 'Content-Type: application/json' \
  -d @examples/quote_zero_for_one.json

# 3) 报价：one -> zero
curl -s -X POST http://127.0.0.1:8000/pools/weth-usdc-30bp/quote \
  -H 'Content-Type: application/json' \
  -d @examples/quote_one_for_zero.json

# 4) 取出已存报价
curl -s http://127.0.0.1:8000/quotes/<quote_id>

# 5) 重新计算并校验摘要、tick 映射、引擎↔参考一致性
curl -s -X POST http://127.0.0.1:8000/quotes/<quote_id>/verify
```

`POST /quote` 的响应里自带 `verification`：服务端在返回前用慢速参考实现核对**每一段**，
若不一致直接返回 500 并列出差异（不会对失败说谎）。

### 端点一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康与常量 |
| POST | `/pools` | 创建不可变池快照（重复 id 返回 409） |
| GET | `/pools` | 列出池 |
| GET | `/pools/{pool_id}` | 池详情：区间表、价格映射、舍入说明、摘要 |
| POST | `/pools/{pool_id}/quote` | 报价（含参考实现交叉验证），落库 |
| GET | `/quotes/{quote_id}` | 取回历史报价与证据 |
| POST | `/quotes/{quote_id}/verify` | 重算 SHA-256、tick 映射、参考一致性 |

---

## 6. 验收命令

```bash
cd /home/admin/Downloads/biaozhul/P012/a
. .venv/bin/activate

# A. 全量自动化测试（59 passed）：正反向跨界、边界 tick、极小流动性、最大输入、
#    空区间、费用恒等、守恒、快照不变、SHA-256 防篡改、HTTP 全链路
python -m pytest

# B. 静态检查（无未用导入/未定义名）
python -m pyflakes app tests

# C. 不启服务的端到端引擎演示（每笔都过 Fraction/Decimal 参考核对）
python -m examples.demo

# D. 起真实 HTTP 服务做端到端 curl 验收
uvicorn app.main:app --port 8000
# 另一终端：
curl -s localhost:8000/health
curl -s -X POST localhost:8000/pools -H 'Content-Type: application/json' -d @examples/create_pool.json
curl -s -X POST localhost:8000/pools/weth-usdc-30bp/quote \
     -H 'Content-Type: application/json' -d @examples/quote_zero_for_one.json
```

测试覆盖的关键场景：

* **正/反向跨界**：多区间顺序穿越，证据链 `上段终点 == 下段起点`；
* **边界 tick**：当前 tick 恰好等于持仓上/下边界时的两个方向；
* **极小流动性**：`L = 1`、输入为 1 wei（dust，原样返回）；
* **最大输入**：`amount_in = 10**80`、流动性铺到宇宙边缘，必终止且守恒；
* **空区间**：完全空池与"成交若干段后遇 1-tick 空隙"，停止并返回未成交输入；
* **180+ 随机微分对拍**（`tests/test_engine.py`）与宽 tick 范围 + 随机空隙对拍
  （`tests/test_edge_cases.py`），全部逐段与 Fraction 参考一致。

---

## 7. 设计取舍与已验证事实

* **热点路径纯整数**；`Fraction`/`Decimal` 只出现在参考验证与人类可读展示中。
* 最后一段用对"整数化毛输入"单调谓词的**二分搜索**求最大可承受价格移动，
  避免在 10³⁸ 定点粒度上逐单位扫描。
* 跨界后按**显式区间索引 ±1** 步进（共享边界价格即下一区间的入点），
  不依赖非满射的"价格反查 tick"，因此不会抖动或死循环。
* 边界 sqrt 价在**建池时一次性预算**并挂在快照上，报价热路径零 tick 映射开销。
* 已用 200 位 Decimal 开方独立确认：整数 tick 映射在 `±100_000` 上与
  `floor(10³⁸·1.0001^(i/2))` 完全相等（递推连乘方案因累积误差被弃用）。
* 计算（AMM、费用、二分）、协议（FastAPI/SQLite）、密码操作（SHA-256）均为真实执行；
  任何引擎与参考不一致都会如实体现在 `verification.discrepancies` 与 500 响应中。
