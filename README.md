# CryptoLaunch — 本地模拟撮合引擎

一个**完全本地化、模拟**的加密货币现货撮合引擎，基于 Django REST Framework + MySQL +
Docker Compose。**不连接区块链、不涉及真实资金、不接入外部行情**；所有资产都是
初始化时铸造的模拟余额。

支持限价单 / 市价单、价格优先 + 同价时间优先撮合、部分成交、手续费、追加式双分录
账本、幂等下单、并发安全、撤单、维护模式、内存订单簿从持久化状态恢复，以及自动对账。

---

## 1. 快速开始（Docker Compose）

```bash
docker compose up --build
```

启动时容器会自动：等待 MySQL → 执行迁移 → 初始化模拟资产/交易对/演示用户 →
从数据库恢复内存订单簿 → 跑一次对账 → 启动 gunicorn。

- API: http://localhost:8000/api/
- 端口冲突时可覆盖：`API_PORT=8327 MYSQL_PORT_PUBLISHED=33277 docker compose up`

### 演示账号（由 seed 命令创建）

| 角色 | 用户名 | 密码 |
|---|---|---|
| 管理员（配费率/维护模式/看审计） | `admin` | `admin12345` |
| 交易用户 | `alice` / `bob` / `carol` | `demo12345` |

演示用户初始持有模拟资产（USDT / BTC / ETH）。

### 不用 Docker（本地运行，可切 SQLite）

```bash
pip install -r requirements.txt

# MySQL（需先有数据库）
export MYSQL_HOST=127.0.0.1
python manage.py migrate
python manage.py seed_simulated
python manage.py warm_books
gunicorn config.wsgi:application --workers 1 --threads 8

# 或纯 SQLite 快速体验 / 跑测试
DB_ENGINE=sqlite python manage.py migrate
DB_ENGINE=sqlite python manage.py seed_simulated
DB_ENGINE=sqlite python manage.py runserver
```

> 生产形态建议 **1 个 gunicorn worker + N 个线程**（订单簿缓存位于进程内）。
> 正确性并不依赖单进程：每次撮合都在市场行锁内以数据库为准重建簿，多 worker
> 只是会多做几次重建、变慢而已。

---

## 2. 核心规则

### 2.1 撮合优先级
1. **价格优先**：买吃最低卖价，卖吃最高买价。
2. **同价时间优先（FIFO）**：`(created_at, sequence)`，其中 `sequence = 订单自增 id`。
3. 限价单可成交、可部分成交；剩余挂入订单簿。
4. **市价单不挂单**：
   - 市价卖：按数量吃对手盘，**剩余数量立即取消**并释放冻结的 base。
   - 市价买：用 `quote_amount`（报价货币额度）吃最便宜的卖盘，剩余 quote 立即
     退还，订单结束（FILLED 或 CANCELED）。

### 2.2 定点数与舍入（8 位小数）

所有金额/数量都是 `Decimal(30,8)`，全程不使用 float。

| 项目 | 舍入 |
|---|---|
| 用户输入价格/数量 | 校验最多 8 位小数 |
| 单笔成交 quote 金额 `price × qty` | **ROUND_FLOOR（向下）** 到 8 位 |
| 手续费 `notional × bps / 10000` | **ROUND_HALF_UP（四舍五入）** 到 8 位 |
| 限价买单冻结额 `price × qty` | **ROUND_HALF_UP**（只舍入一次） |
| 市价买可承受数量 `预算 / 价` | **ROUND_FLOOR**，保证花费绝不超过冻结 |

为什么成交价向下取整：`Σ floor(xᵢ) ≤ floor(Σxᵢ) ≤ HALF_UP(总额)`，因此买家各笔
quote 扣款之和**永远不超过**下单时一次性 HALF_UP 冻结的额度，舍入不会把余额
扣成负数；卖家只是拿不到小于 `1e-8` 的 quote 零头，不会凭空产生资产。

### 2.3 手续费
- 每个交易对两个费率，单位为**基点 bps**（`10 bps = 0.10%`，`10000 bps = 100%`），
  管理员可配，限制 `[0, 10000]`。默认 maker 10 bps、taker 20 bps。
- 手续费从**各方实际收到的资产**中扣除：买方从收到的 base 扣、卖方从收到的
  quote 扣；手续费进入系统账户 `fee-account`，并参与对账。
- 例：`1 BTC @ 50000`，taker 买费 20bps → 买得 `1 − 0.002 = 0.998 BTC`；
  maker 卖费 10bps → 卖得 `50000 − 50 = 49950 USDT`。

### 2.4 冻结 / 成交 / 撤单的资产守恒
- 下单即冻结所需资产（卖冻 base 数量，买冻 quote 额度），记录 `FREEZE` 账本事件。
- 成交在**一个数据库事务**内原子更新：双方余额（available/frozen）、两张订单、
  Trade、每方 Fill、以及一笔**借贷平衡**的 `TRADE` 双分录事件。
- 撤单释放剩余冻结（`RELEASE` 事件）。
- 余额表带数据库级 `CHECK (>=0)` 约束（唯一例外是充当模拟供应来源的
  `sim-genesis` 权益账户，用表内 `is_equity` 标志放行负 available）。
  任何时刻都不能出现真实用户或手续费账户负余额，也不能凭空增减资产。

### 2.5 并发正确性
- 同一交易对的下单/撮合/撤单/维护，全部在该 `markets` 行上
  `SELECT … FOR UPDATE` 串行化；不同交易对互不阻塞、可并行。
- 撮合前在市场锁内**以数据库为准重建内存簿**，消除线程缓存的读陈旧
  （fill 与 cancel 竞争时，后拿到锁的一方一定看到已提交结果）。
- 每个挂单在吃单前再次 `select_for_update` 锁定并复查状态，
  **成交与撤单竞争只有一种合法结果**：成交，或撤单，不会两者都发生。
- 余额行按全局 `(user_id, asset_id)` 顺序加锁，避免 AB/BA 死锁。
- 遇到 InnoDB 死锁(1213)/锁等待超时(1205) 时整体事务回滚并安全重试
  （`apps/trading/retry.py`，API 已内置）。

### 2.6 幂等键
- 请求头/体可带 `idempotency_key`。键按用户隔离，存请求体 SHA-256。
- 同键同参数重复提交 → 返回原订单（`replayed=true`），不重复冻结/下单。
- 同键不同参数 → **409 Conflict**，并写审计。
- 并发同键：市场锁内复查 + 数据库唯一约束双保险，保证只产生一张订单。

### 2.7 崩溃恢复 / 防重复结算
- 提交前崩溃：整个事务（含冻结）回滚，不存在“有单无冻结”或“有成交无结算”。
- `Fill(trade, order)` 唯一约束 + 订单进度更新与成交同事务 →
  **中断后不可能对同一笔成交重复结算**。
- 重启后 `warm_books`（或首次访问惰性）从持久化的工作订单
  `(status, price, sequence)` 重建簿，**严格保持原价格-时间顺序**。
- 簿缓存只在事务提交后失效/重建，绝不与已提交状态背离。

### 2.8 维护模式
- 进入维护：停止接单（提交返回 400）、**撤销所有挂单并释放全部冻结**。
- 可重复执行：再次“进入”为空操作（幂等）；“退出”后恢复交易。
- 整个切换在一个事务内，并写 `MAINTENANCE` 账本事件与审计日志。

### 2.9 权限与审计
- 用户只能查看/操作**自己**的余额、订单、成交（列表与对象级都做了隔离）。
- 管理员（`is_staff`）才能建交易对、改费率、切维护模式、查审计日志。
- 关键操作（注册、登录、改费率、维护切换、建交易对、模拟充值、拒单、
  幂等冲突）落 `audit_logs`。

---

## 3. HTTP API 速览

除注册/登录/行情/订单簿外均需 `Authorization: Token <key>`。

```bash
# 注册 / 登录拿 Token
curl -X POST localhost:8000/api/auth/register/ \
  -d '{"username":"u1","password":"secret123"}'
curl -u u1:secret123 -X POST localhost:8000/api/auth/login/

# 限价单（卖冻 base）
curl -H "Authorization: Token $T" -H 'Content-Type: application/json' -X POST \
  localhost:8000/api/orders/ -d '{
    "symbol":"BTC-USDT","type":"LIMIT","side":"SELL",
    "price":"50000","quantity":"0.5","idempotency_key":"abc"}'

# 市价买（花 quote，数量字段不用填）
curl ... -d '{"symbol":"BTC-USDT","type":"MARKET","side":"BUY",
              "quote_amount":"1000"}'
# 市价卖
curl ... -d '{"symbol":"BTC-USDT","type":"MARKET","side":"SELL",
              "quantity":"0.1"}'

# 查自己的订单 / 撤单 / 成交 / 余额 / 订单簿
GET  /api/orders/?symbol=BTC-USDT&status=NEW
GET  /api/orders/{id}/
DELETE /api/orders/{id}/
GET  /api/trades/
GET  /api/balances/
GET  /api/markets/
GET  /api/markets/BTC-USDT/orderbook/?depth=50

# 管理员
POST   /api/admin/markets/                         # 建交易对
PATCH  /api/admin/markets/BTC-USDT/fees/           # 改 maker/taker bps
POST   /api/admin/markets/BTC-USDT/maintenance/    # {"enabled": true/false}
GET    /api/admin/audit-logs/?action=FEE_UPDATE
```

错误码：资金不足/维护中/参数非法 → 400；同键不同参数、撤已终结单 → 409；
非本人资源 → 404（不泄露存在性）。

---

## 4. 管理命令与对账

```bash
python manage.py seed_simulated [--demo-users 3]   # 幂等初始化模拟数据
python manage.py warm_books                         # 启动时恢复订单簿
python manage.py reconcile [--quiet]                # 对账，失败退出码 1
```

`reconcile` 做四项检查（手续费账户也在内）：

1. **账本守恒**：每个资产全部分录有借必有贷，求和为 0
   （模拟铸造是 `sim-genesis` 权益 → `sim-bank` 的平衡分录）。
2. **账本↔余额**：按 `(用户,资产,available/frozen)` 重放分录，必须与当前
   `balances` 表逐行相等。
3. **冻结完整性**：所有工作订单 `frozen_remaining` 之和必须等于对应
   用户/资产的 `balance.frozen`。
4. **非负余额**：除 `sim-genesis` 权益账户外，任何账户不得为负。

建议把 `reconcile` 放进定时任务/监控，非零退出即告警。

### 双分录账本模型
- `ledger_events`：一次余额变动一个事件（FREEZE / RELEASE / TRADE /
  DEPOSIT / MAINTENANCE），**只追加、不修改不删除**。
- `ledger_entries`：事件下若干带符号分录，正数=入账、负数=出账，
  每个资产内事件分录之和恒为 0；每条还记录变动后余额便于审计追踪。
- `trades` + `fills`：成交与每方成交记录（append-only），Fill 唯一约束
  防止重复结算。

---

## 5. 运行测试

```bash
# SQLite（快速；3 个真实线程竞态用例会自动 skip）
DB_ENGINE=sqlite python manage.py test tests

# MySQL（跑全部，包括并发）
export DB_ENGINE=mysql MYSQL_HOST=127.0.0.1
# 需要建库权限：
#   GRANT ALL ON `test_%`.* TO cryptolaunch@'%';
python manage.py test tests -v1
```

测试覆盖（43 个）：

| 文件 | 内容 |
|---|---|
| `test_matching.py` | 全成/部分成交、价格优先、同价 FIFO、限价剩余挂单与冻结校正、市价买卖、剩余立即取消、资金不足拒单、维护拒单 |
| `test_idempotency.py` | 同键重放不重复下单/冻结、同键异参 409、键按用户隔离 |
| `test_cancel.py` | 撤单释放、重复撤单不重复释放、撤已成交单冲突、部分成交后只释放剩余、不能撤他人单，以及 MySQL 成交/撤单并发竞态 |
| `test_rounding.py` | 8 位小数边界、FLOOR 成交价、HALF_UP 手续费、dust 价格下买家不为负、手续费账户归集 |
| `test_maintenance.py` | 进入维护撤全部挂单并释放、幂等、拒单、退出恢复 |
| `test_recovery.py` | 重启后簿保持价格-时间顺序（含部分成交后）、恢复后正确撮合、已撤/市价单不入簿 |
| `test_concurrency.py` | MySQL：20 并发买 vs 5 BTC 薄盘（无负余额/无孤儿冻结/守恒/对账通过）、8 线程同幂等键只下一单 |
| `test_reconcile.py` | 多笔成交/撤单/市价后对账通过、挂单冻结一致性 |
| `test_api.py` | 注册登录、HTTP 下单成交撤单生命周期、用户隔离、幂等、管理员费率/维护/审计、订单簿、未知交易对 404 |

---

## 6. 目录结构

```
config/                 Django 项目（settings/urls/wsgi）
apps/
  common/decimals.py    定点数与舍入规则
  accounts/             用户、资产、余额(CHECK>=0)、冻结/释放/结算原语、seed 命令
  markets/              交易对、费率、维护标志、管理员序列化
  trading/              订单/成交/Fill/幂等键、订单簿、撮合引擎、撤单、维护、重试、API
  ledger/               追加式双分录账本、对账命令
  audit/                审计日志与管理员查询 API
scripts/entrypoint.sh   容器启动脚本
docker-compose.yml      MySQL 8.4 + API(1 worker/8 threads)
tests/                  43 个测试
```
