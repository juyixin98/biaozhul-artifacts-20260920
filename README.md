# CryptoLaunch 本地模拟撮合引擎

一个**纯本地模拟**的数字资产撮合交易服务：Django REST Framework + MySQL + Docker Compose。
不接区块链、不接真实资金、不接外部行情，所有资产均由模拟发行账户记账产生。

## 功能一览

- 限价单 / 市价单；**价格优先、同价时间优先（FIFO）**撮合，支持部分成交；
  市价单吃不完的剩余数量立即取消，剩余冻结原路退回。
- 金额、数量、价格全部 `Decimal` 定点数，统一 **8 位小数**。
- 下单即冻结资产；成交在**单笔数据库事务**里原子更新双方余额、订单、成交记录与
  **追加式双分录账本**；撤单释放剩余冻结。任何时刻用户/手续费账户不得为负。
- 同一交易对并发安全（交易对级互斥锁 + 数据库行锁双重保障）；
  下单支持**幂等键**：重复键不重复下单，同键不同参数返回 `409`。
- 内存订单簿可从数据库按 `(价格, id)` 重建，保留原价格-时间顺序；
  每笔成交先提交数据库再更新内存，崩溃恢复不会重复结算。
- 维护模式：停止接单 → 撤销全部挂单 → 释放冻结；重复进入安全。
- 用户只能查看自己的资产/订单/成交；管理员可配置费率、切换维护、查看审计与对账。

## 目录结构

```
accounts/      资产、账户、双分录账本、模拟发行、对账
trading/       交易对/订单/成交模型、内存订单簿(orderbook.py)、撮合引擎(engine.py)
cryptolaunch/  项目配置（wsgi 加载时自动重建订单簿）
scripts/       手工冒烟脚本
trading/tests/ 业务/舍入/维护恢复/并发/API 测试
```

## 快速启动（Docker Compose）

```bash
docker compose up --build
```

启动后：
- API： http://localhost:8000/api/
- MySQL：宿主机 127.0.0.1:13306（库 `cryptolaunch`，用户 `cryptolaunch`）
- 容器入口会自动：等待数据库 → `migrate` → `init_sim_data` → 启动 gunicorn

> 生产形态为 1 个 gunicorn worker + 多线程（内存订单簿是进程内状态）。

### 模拟数据（init_sim_data 自动执行）

| 用户 | 口令 | 角色 | 初始资产 |
|---|---|---|---|
| admin | admin123 | 管理员 | — |
| alice | trade123 | 普通 | 1,000,000 USDT / 10 BTC / 100 ETH |
| bob | trade123 | 普通 | 1,000,000 USDT / 10 BTC / 100 ETH |

交易对：`BTCUSDT`（tick 0.01，lot 0.000001，最小金额 10）、
`ETHUSDT`（tick 0.01，lot 0.0001，最小金额 5）。

### 无 Docker 本地运行（SQLite）

```bash
python3 -m venv .venv && source .venv/bin/pip install -r requirements.txt
export DB_ENGINE=sqlite DB_NAME=db.sqlite3
python manage.py migrate
python manage.py init_sim_data
python manage.py runserver
```

## 定点数、冻结与手续费规则

所有计算禁止 `float`，统一走 `trading/decimal_utils.py`：

| 场景 | 规则 | 舍入 |
|---|---|---|
| 用户价格 / 数量 | 必须分别是 `tick_size` / `lot_size` 的整数倍 | 精确匹配，否则 400 |
| 下单冻结（限价买） | `ceil8(限价 × 数量)` | **ROUND_CEILING**（多冻，防透支） |
| 下单冻结（市价买） | 委托报价币预算 | 原值（8 位） |
| 下单冻结（卖单） | 委托 base 数量 | 原值 |
| 单笔成交额 | `floor8(成交价 × 数量)` | **ROUND_FLOOR**（保证逐笔累计不击穿买方冻结） |
| 手续费 | `floor8(计费基数 × 费率)` | **ROUND_FLOOR**，不足 1e-8 按 0 |

**手续费从“收到的资产”里扣除**（费率默认 taker=maker=0.1%，管理员可配）：

- 买入：收到 base，手续费扣 base；卖出：收到 quote，手续费扣 quote。
- 例：买 1 BTC @50000，taker 到手 `0.999 BTC`；对手卖单到手 `49950 USDT`。

**粉尘（dust）处理**：限价买 `CEILING` 冻结与 `FLOOR` 成交额之间的零头（< 1e-8/笔），
在订单部分成交重挂、全部成交、撤单时统一退回可用余额；卖方按数量冻结无粉尘。

## 双分录账本与对账

- 每一次真实权属变化写一组 `LedgerEntry`（同 `tx_group`），按资产求和恒为 0：
  - 模拟发行：用户 +X，FUNDING 发行账户 −X；
  - 成交：买方/卖方/手续费账户多腿分录，`quote 流出 = 卖方到手 + 手续费`、
    `base 流出 = 买方到手 + 手续费`。
- 冻结/解冻只是 `available ↔ frozen` 内部划转，不进账本（资产权属未变）。
- 账本**只追加**：服务层不提供 update/delete 路径。

运行对账：

```bash
docker compose exec api python manage.py reconcile
# 或管理接口：GET /api/admin/reconcile
```

对账四项：分录组守恒、全局资产守恒（含 FEE 手续费账户）、账实一致（账本累加=账户余额）、
挂单冻结之和=账户 frozen。

## API 摘要

认证：`Authorization: Token <key>`（注册/登录返回 token）。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/auth/register` `/api/auth/login` | 注册 / 登录 |
| GET | `/api/accounts/me` | 我的资产 |
| GET | `/api/accounts/me/ledger` | 我的账本流水 |
| GET | `/api/pairs` | 交易对列表 |
| POST | `/api/orders` | 下单（可带 `idempotency_key`） |
| GET | `/api/orders/mine` `/api/orders/{id}` | 我的订单 / 详情 |
| POST | `/api/orders/{id}/cancel` | 撤单 |
| GET | `/api/trades/mine` | 我的成交 |
| GET/PATCH | `/api/admin/config` | 查/改 taker、maker 费率（管理员） |
| POST | `/api/admin/maintenance/on` `/off` | 维护切换（管理员） |
| GET | `/api/admin/audit` | 审计日志（管理员） |
| GET | `/api/admin/reconcile` | 对账（管理员） |

下单请求示例：

```json
{
  "pair": "BTCUSDT",
  "side": "BUY",
  "type": "LIMIT",
  "quantity": "0.50000000",
  "limit_price": "50000.00",
  "idempotency_key": "client-uuid-123"
}
```

市价买入时 `quantity` 是报价币预算（如 `30000` USDT），`limit_price` 省略；
市价卖出时 `quantity` 是 base 数量。

### 手工冒烟

```bash
# SQLite
DB_ENGINE=sqlite DB_NAME=db.sqlite3 python manage.py migrate
DB_ENGINE=sqlite DB_NAME=db.sqlite3 python manage.py init_sim_data
DB_ENGINE=sqlite DB_NAME=db.sqlite3 PYTHONPATH=. python scripts/smoke.py
```

## 测试

```bash
# 全部（SQLite 会自动跳过并发用例）
DB_ENGINE=sqlite DB_NAME=:memory: python manage.py test trading

# 完整含真并发（需要 MySQL，见下方环境变量）
DB_ENGINE=mysql DB_HOST=127.0.0.1 DB_PORT=13306 \
DB_NAME=cryptolaunch DB_USER=cl DB_PASSWORD=clpw \
python manage.py test trading
```

覆盖：部分成交 / 价格与时间优先 / 市价剩余取消 / 并发撤单与成交竞争（唯一合法结果）/
10 线程并发吃单不超卖 / 舍入（冻结、成交额、手续费、粉尘）/ 幂等重复与冲突 /
维护切换撤光与释放（重复安全）/ 重启恢复价格时间顺序与不重复结算 / API 权限与状态码。

## 崩溃恢复语义

1. 成交事务提交前崩溃：事务回滚，余额/订单/账本均无该笔，重启后簿按订单状态重建即可；
2. 提交后、内存簿更新前崩溃：重启时以数据库终态重建内存簿（已成交 maker 不在簿），
   账本中该笔恰好存在一次，不会重复结算；
3. 重启入口在 `cryptolaunch/wsgi.py`：应用初始化后调用 `bootstrap_order_book()`。
