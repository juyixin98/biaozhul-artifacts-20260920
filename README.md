# 集中流动性区间计算（离线报价引擎）

纯后端服务：Python 3.12 + FastAPI + SQLite。实现一个 **Uniswap v3 风格、
但全部参数为本项目自定义定点协议**的集中流动性报价引擎：整数 tick 网格、
Q96 定点 sqrtPrice、跨多个流动性区间逐段扣费、空区间按规则停止并返回
未成交输入、报价绑定不可变池快照、HMAC-SHA256 签名证据。

报价是**只读纯函数**：不修改储备，池没有可被报价改变的余额状态。

---

## 1. 定点协议与价格映射

| 项 | 定义 |
|---|---|
| 价格语义 | `P = (sqrtPriceX96 / 2^96)^2`，单位 token1/token0 |
| tick 关系 | `P(t) = 1.0001^t`，`sqrtP(t) = (10001/10000)^(t/2)` |
| 整数 tick 范围 | `[-887272, 887272]`（闭区间） |
| tick 边界 | `sqrtPriceX96(t) = ceil(2^96 * sqrtP(t))` |
| tick 反查 | `tick_of(s) = max { t : sqrtPriceX96(t) <= s }`（floor） |
| 费用 | `fee_ppm`，百万分之一；合法区间 `[0, 999999]`（**100% 费用被拒绝**） |

边界值与公认参考实现完全一致：

- `sqrtPriceX96(MIN_TICK) = 4295128739`
- `sqrtPriceX96(0) = 2^96 = 79228162514264337593543950336`
- `sqrtPriceX96(MAX_TICK) = 1461446703485210103244672773810124308346321380903`

## 2. 舍入方向（显式、对池子/协议有利）

| 量 | 舍入 |
|---|---|
| 段费用 fee | **ceil**（向上，协议不吃亏） |
| 段本金 principal | `gross - fee`（隐式 floor） |
| 跨 tick 所需本金容量 | **ceil**（向上） |
| 段输出 amount_out | **floor**（向下） |
| 段内部分成交终价 sqrtPrice | **朝段内夹持**：下行 ceil、上行 floor（不越过真实终点） |
| 未成交输入 | **原样返回，不计任何费用** |

每段与总计都强制对账恒等式：

```
amount_in == Σ fee + Σ principal + amount_in_unfilled
```

段内另强制 `gross == principal + fee`。引擎在每次报价末尾用断言复核。

### 除零与退化防护

- `L == 0` 的区间**不进入任何除法**：直接按 `incomplete_gap` 停止；
- `fee_ppm == 1_000_000`（100% 费用，本金恒为 0）在建池时拒绝；
- 所有 sqrtPrice 均为正整数，MIN/MAX 网格边界独立于流动性先判断，
  走到 `MIN_TICK/MAX_TICK` 按 `incomplete_edge` 停止；
- 段数安全上限 `MAX_SWAP_STEPS`，防止异常数据造成无限循环。

## 3. 目录结构

```
app/
  clmm/
    constants.py    # 定点协议常量与停止原因
    pricing.py      # tick <-> sqrtPriceX96 精确整数映射
    engine.py       # 纯函数报价引擎（逐段扣费、空区间、对账断言）
    decimalref.py   # Decimal 慢速独立参考实现（测试交叉验证用）
    crypto.py       # SHA-256 快照摘要、HMAC-SHA256 签名/验签
    schemas.py      # Pydantic 请求模型
  db.py             # SQLite 只追加存储
  service.py        # 快照组装、引擎调用、签名绑定
  main.py           # FastAPI 路由
tests/              # 64 个自动化测试
examples/           # 示例请求与离线演示脚本
requirements.txt    # 直接依赖（锁定版本）
requirements.lock.txt  # 完整传递依赖锁定
```

## 4. 本地启动

需要 Python 3.12（标准库 sqlite3）。

```bash
cd /home/admin/Downloads/biaozhul/P012/b
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.lock.txt   # 或 pip install -r requirements.txt

# 建议显式设置 HMAC 密钥；不设时使用开发默认密钥（/health 会告警）
export CLMM_HMAC_KEY="$(python3 -c 'import secrets;print(secrets.token_hex(32))')"
export CLMM_DB_PATH="$PWD/clmm.db"     # 可选，默认 ./clmm.db

uvicorn app.main:app --host 127.0.0.1 --port 8000
```

健康检查：`curl http://127.0.0.1:8000/health`
交互文档：`http://127.0.0.1:8000/docs`

## 5. API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET  | `/health` | 健康与 HMAC 密钥状态 |
| POST | `/pools` | 创建不可变池快照（重复 pool_id → 400） |
| GET  | `/pools` | 池列表 |
| GET  | `/pools/{id}` | 池信息 |
| GET  | `/pools/{id}/snapshot` | 含 SHA-256 的规范快照 |
| POST | `/quotes` | 报价；返回完整逐段证据 + HMAC 签名 |
| GET  | `/quotes/{quote_id}` | 取回历史报价（只追加） |
| POST | `/quotes/verify` | 校验报价负载签名 |

数量字段（`amount_in`、`liquidity`、`sqrt_price_x96`）一律以**十进制字符串**
传输，避免 JSON/JS 大整数精度问题。

停止原因：

- `filled`：输入全部成交；
- `incomplete_gap`：前方空（零流动性）区间，剩余输入原样返回；
- `incomplete_edge`：到达 MIN/MAX tick 网格尽头；
- `incomplete_limit`：到达调用方 `limit_tick`。

### 快速验收（HTTP）

```bash
curl -s -X POST localhost:8000/pools -H 'Content-Type: application/json' \
  -d @examples/create_pool.json

curl -s -X POST localhost:8000/quotes -H 'Content-Type: application/json' \
  -d @examples/quote_up.json
```

### 离线演示（不起服务）

```bash
python examples/demo_offline.py
```

覆盖：段内成交、跨多区间逐段扣费、limit_tick、空区间原样返回、
极小流动性 L=1、超大输入对账。

## 6. 验收命令

```bash
source .venv/bin/activate
python -m pytest -v          # 64 个测试，约 1-2 秒
python examples/demo_offline.py
```

测试覆盖（见 `tests/`）：

1. **价格映射**（`test_pricing.py`）：ceil 边界与独立 Decimal 核验、
   floor 反查、严格单调、MIN/MAX 极值与公认常量比对、越界拒绝；
2. **引擎**（`test_engine.py`）：ceil 费用、对账恒等式、零费用、
   1 单位输入、空区间立即停止且不计费、跨多区间逐段留证、
   边界 tick（MIN/MAX/0/±1）、L=1 极小流动性、10^70 最大输入不溢出、
   limit_tick、快照不可变；
3. **Decimal 慢速参考交叉验证**（`test_decimal_reference.py`）：
   第二套独立实现（独立 Decimal 求幂边界，不调用引擎整数边界），
   对正反向跨界、边界 tick、极小流动性、最大输入、limit_tick、
   60 组随机池做**逐段精确比对**（费用/本金/输出/终价/停止原因全等），
   并验证连续模型下整数引擎不高估输出、低估量严格有界；
4. **密码学**（`test_crypto.py`）：SHA-256 标准向量、规范 JSON 确定性、
   快照逐字段敏感、HMAC 往返、篡改/伪签名拒绝；
5. **API**（`test_api.py`）：建池、报价绑定快照、验签、篡改拒绝、
   只追加持久化与回放、报价不改池、404/400/422、空区间返回未成交输入。

## 7. 密码操作

均为真实执行（`hashlib` / `hmac`，无桩、无模拟）：

- 池快照：对规范 JSON（键排序、无空白、`ensure_ascii=False`）算 **SHA-256**；
- 报价：负载内嵌 `snapshot_hash`，整体做 **HMAC-SHA256**；
- 验签：`hmac.compare_digest` 常量时间比较（防时序侧信道）。
