# 借贷清算回放器 (Lending / Liquidation Replayer)

纯后端**离线**抵押借贷回放服务（Python + FastAPI + PostgreSQL）。价格与
操作均来自本地 **Ed25519 签名事件**，不提交任何真实交易。服务对事件集合
做确定性重算，输出带哈希与服务端签名的**版本化报告**；迟到事件触发历史
重算，旧报告永久保留。

## 协议规则（全部为整数定点，确定性）

* **定点数**：所有金额、价格、利率因子都是带 `WAD = 10^18` 缩放的整数，
  全链路无浮点；除法一律向下取整。
* **时间**：Unix 秒整数；事件按 `(ts, seq, event_id)` 规范化全序回放，
  同 `ts` 严格按 `seq` 升序处理 → 乱序/迟到/分批提交结果完全一致。
* **分段利率（半开区间）**：`RATE_SCHEDULE.effective_ts` 起生效直到下一段；
  分段表在回放前由**全部**利率事件预构建，所以迟到的利率修订也会修正历史。
  区间内单利、边界并入本金，年化按 365 天通用年，每个区间只取整一次：

  ```
  interest = debt * annual_rate * dt // (WAD * 31_536_000)
  ```

* **健康度（严格小于 1 才可清算）**——用跨乘整数比较，`HF == 1` 不清算：

  ```
  HF = collateral * price * threshold / (debt * WAD^2)
  ```

* **单次最多偿还 50% 债务**：请求 `repay > floor(debt/2)` 直接拒绝
  （`EXCEEDS_50_PERCENT`），不做静默截断；同一仓位可被反复清算。
* **清算奖励 8%**（`seize = repay * 1.08 / price`，单次向下取整）。
  抵押不足时按可用抵押**比例足额成交**：
  `repay = collateral * price / 1.08`（向下取整）、收走全部抵押；
  取整后为 0 拒绝（`DUST`）。
* **陈旧预言机**：价格年龄**严格大于 60 秒**即陈旧 → 拒绝清算
  （`STALE_PRICE`），但利息照常累计。60 秒边界上仍视为新鲜。

## 事件（POST /api/v1/events，批量）

信封：

```json
{
  "event_id": "ev-1", "ts": 1700000000, "seq": 0,
  "action": "OPEN | ORACLE | RATE_SCHEDULE | REPAY | LIQUIDATE",
  "payload": { ... },
  "signer": "<受信任操作者 Ed25519 公钥 hex>",
  "signature": "<对其余五个字段规范化 JSON 的 Ed25519 签名 hex>"
}
```

动作载荷（金额均为 WAD 整数）：

| action | payload |
|---|---|
| `OPEN` | `position_id, asset, collateral, debt, liquidation_threshold`（阈值 ≤ 1e18） |
| `ORACLE` | `asset, price`（事件时间戳即价格时间戳） |
| `RATE_SCHEDULE` | `effective_ts, annual_rate_wad`（年化，如 0.1 = 1e17） |
| `REPAY` | `position_id, amount`（超过当前债务拒绝 `OVERPAY`） |
| `LIQUIDATE` | `position_id, repay` |

验签失败（`UNTRUSTED_SIGNER` / `BAD_SIGNATURE`）的事件不入存储；业务拒绝
（健康、陈旧、超额等）事件**仍入存储**并在报告 `actions` 中带原因留痕，
保证回放可复现。重复 `event_id` 幂等忽略。

## API

| 方法/路径 | 说明 |
|---|---|
| `POST /api/v1/events` | 提交一批签名事件；去重 → 验签 → 全量重算 →（按需）生成新版本 |
| `GET /api/v1/reports/latest` | 最新版本完整报告（仓位、价格、分段、清算、动作、哈希） |
| `GET /api/v1/reports` | 版本摘要列表（旧版本均保留） |
| `GET /api/v1/reports/{v}` | 取指定历史版本 |
| `GET /api/v1/reports/{v}/verify` | 用服务端公钥验报告 Ed25519 签名并重算哈希 |
| `GET /api/v1/liquidations` | 最新报告中的全部已成交清算 |
| `GET /api/v1/replay?as_of=` | 只读回放（不生成版本） |
| `GET /health` · `GET /` · `/docs` | 健康检查、信息、Swagger |

报告版本只在输出哈希变化时新建；原因标记为 `INITIAL` / `NEW_EVENTS` /
`LATE_EVENT_RECOMPUTE`。响应里 `changed_reasons` 会列出迟到事件 id。

## 目录结构

```
app/            FastAPI + 引擎 + 仓储 + 密码学
  engine.py       确定性回放（定点利息/健康度/清算/规范化排序/报告哈希）
  fixedpoint.py   1e18 整数定点工具
  crypto.py       规范化 JSON + Ed25519 真实签名/验签（cryptography）
  service.py      验签→去重→重算→版本化→签名
  repository.py   PostgresRepo（只追加）与 MemoryRepo（测试）
  main.py         路由
examples/events.py  示例输入（含利率切换/两次清算/部分成交/陈旧价格）
scripts/generate_keys.py  生成操作者 Ed25519 密钥
scripts/seed.py           签名并提交示例事件
tests/          36 个自动化测试（引擎/密码/服务/HTTP/PostgreSQL）
acceptance.sh   一键本地验收
requirements.txt / requirements.lock
```

## 本地启动

前置：Python 3.12、Docker（用于 Postgres，或自备一个 PG）。

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

docker run -d --name liqreplay-db \
  -e POSTGRES_USER=liqreplay -e POSTGRES_PASSWORD=liqreplay \
  -e POSTGRES_DB=liqreplay -p 55460:5432 postgres:16-alpine

python scripts/generate_keys.py        # 生成 data/keys/operator_ed25519.pem
export LIQREPLAY_OPERATOR_KEYS="<打印出的公钥 hex>"
export LIQREPLAY_DATABASE_URL="postgresql://liqreplay:liqreplay@127.0.0.1:55460/liqreplay"
uvicorn app.main:app --reload
```

服务端报告签名密钥在首次启动时自动生成于 `data/keys/server_ed25519.pem`
（可用 `GET /` 的 `server_public_hex` 取得公钥，或在 `/api/v1/reports/1/verify` 验证）。

提交示例并查看：

```bash
python scripts/seed.py
curl -s http://127.0.0.1:8000/api/v1/reports/latest | jq .
curl -s http://127.0.0.1:8000/api/v1/reports/1/verify | jq .
```

## 验收命令

一键端到端（起库 → 安装 → **36 个测试（含真实 PostgreSQL）** → 起服务 →
签名播种 → 断言两次清算、抵押不足部分成交、`STALE_PRICE`、版本验签）：

```bash
./acceptance.sh
```

只跑测试：

```bash
pytest                                            # 33 个（内存仓储，无需数据库）
LIQREPLAY_TEST_DATABASE_URL=postgresql://liqreplay:liqreplay@127.0.0.1:55460/liqreplay \
  pytest                                          # 36 个（追加 PostgreSQL 仓储测试）
```

测试覆盖的验收点：利率年化精确累计与切换半开边界、迟到利率修订重算历史、
**健康度恰好 1 不清算** / 低于 1 清算、50% 上限（恰好 50% 允许、超额拒绝）、
同刻多次清算各自受 50% 约束、重复事件幂等、同 `ts` 事件稳定排序且插入顺序
无关（哈希相等）、价格年龄 60/61 秒边界、抵押不足公式、Ed25519 验签与篡改
拒绝、迟到事件生成新版本且旧报告原样保留、两版报告签名均有效。

## 配置（环境变量）

| 变量 | 默认 |
|---|---|
| `LIQREPLAY_DATABASE_URL` | `postgresql://liqreplay:liqreplay@127.0.0.1:55460/liqreplay`（设为 `memory` 用内存仓储） |
| `LIQREPLAY_OPERATOR_KEYS` | 逗号分隔的受信任 Ed25519 公钥 hex |
| `LIQREPLAY_KEYS_DIR` | `data/keys` |
| `LIQREPLAY_STALENESS_SECONDS` | `60` |
| `LIQREPLAY_BONUS_NUM/DEN` | `108/100`（8% 奖励） |

## 安全与边界说明

* 仅做离线回放：服务不连接任何链、不构造交易；密码学操作为真实
  Ed25519（RFC 8032）签名/验签与 SHA-256 内容哈希，无桩实现。
* 原始事件与报告均只追加（append-only），历史不可改写；历史"修正"通过
  追加新版本体现。
* 私钥仅存本地 PEM（0600），示例密钥每次重新生成，勿用于生产。
