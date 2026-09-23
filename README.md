# 借贷清算回放器（Lending Liquidation Replayer）

纯后端的**离线**抵押借贷清算回放服务：价格与操作均来自本地签名事件，
**不提交任何真实链上交易**。服务对事件流做确定性全量回放，输出可验签、
带哈希链、版本永久保留的回放报告。

- Python 3.12 · FastAPI · PostgreSQL（也可零依赖跑进程内内存库）
- 全链路整数定点（WAD = 10¹⁸），浮点不允许进入协议计算
- 真实 Ed25519 密码学（PyNaCl）：事件签名验证 + 报告签名/哈希链
- 34 个自动化测试（33 个内存模式 + 1 个真实 PostgreSQL 集成测试）

---

## 1. 协议规则（明确的边界定义）

### 1.1 时间模型

| 规则 | 定义 |
|---|---|
| 时间单位 | 整数秒；事件字段 `ts` |
| 利息区间 | 半开区间 `[t0, t1)`，共 `t1 - t0` 秒；同一秒 `dt=0` 不计息 |
| 利率切换 | `rate_schedule` 事件在其 `ts` 当秒立即生效，切换点属于新区间 |
| 事件排序 | 全序：`ORDER BY (ts ASC, seq ASC)`；`seq` 为入库顺序。同秒事件结果**稳定可复现** |
| 回放截止 | 报告快照把所有持仓计息到"最大事件时间"（不使用墙钟时间） |

### 1.2 分段利率（固定、线性、非复利）

`rate_schedule` 定义参数：

- `rate_per_second`：第一档年化拆成的**每秒**利率（WAD 定点）
- `tier1_m1`（m1）、`tier2_m2`（m2）：分段阈值；`m2=0` 表示无第二档
- `liq_bonus_num / liq_bonus_den`：清算奖励（如 11/10 = 10%）

每段利息（段内债务恒定，整段一次整除）：

```
tr1 = min(D, m1)
interest  = floor(tr1 * r1 * dt / WAD)
tr2 = max(0, D - m1)
if m2 > 0:                       # 第二档折减利率 r2 = r1 * m1 / m2
    interest += floor(tr2 * r1 * m1 * dt / (WAD * m2))
```

全程 `floor`：系统永远不会凭空多记 1 wei 债务。

### 1.3 健康度与清算

- 抵押价值：`V = Σ floor(balance_asset * price_asset / WAD)`
- 健康度：`H = V * WAD / D`（比较用交叉相乘）。**H < 1 才允许清算；H 恰好等于 1 不可清算**。
- 单次偿还上限：`repay = floor(D * 1/2)`（50%，可用环境变量调整分子分母）。
- 清算奖励：需没收的抵押名义价值 `target = ceil(repay * num/den)`（ceil 保证足额）。

| 情形 | 处理 |
|---|---|
| 抵押充足（V > target） | 按 `asset` 升序贪心没收至 target（最后一档最多过收 ≤1 最小单位），减免债务 `repay` |
| 抵押不足（V ≤ target） | **没收全部抵押**；实际还贷 `effective = floor(V * den/num)`；`shortfall_value = target − V` 如实记录；债务只减免 `effective` |
| 无债务 | 拒绝（`zero_debt`） |
| H ≥ 1 | 拒绝（`healthy`），H==1 同样拒绝 |
| 抵押资产缺价格 | 拒绝（`missing_price`） |
| 价格年龄 **> 60 秒** | **暂停清算**（`stale_price`）；恰好 60 秒仍有效 |

陈旧判定只针对持仓中余额非零的资产。年龄 = 清算 `ts − price_update.ts`。

### 1.4 迟到事件与版本化重算

- 事件表 append-only，永不修改、永不删除。
- 若新批次的最大 `ts` **严格小于**已处理事件的最大 `ts`，判定为迟到，
  触发全量重算，生成新报告版本；**旧报告逐版本永久保留**。
- 新报告通过 `prev_hash = sha256(canonical(旧报告完整体))` 与旧报告形成哈希链。

### 1.5 密码学（真实执行，非模拟）

- 提交方对事件信封 `{event_id, ts, type, payload}` 做 canonical JSON 后 **Ed25519 签名**；
  服务用 feeder 公钥逐事件验签，任何字段被篡改都会被拒（400 `bad_signature`）。
- 服务对**完整报告体**（含 `result`、事件列表、版本、链指针、时间戳）做 sha256 摘要
  并用 server 私钥 Ed25519 签名；`scripts/verify_report.py` 独立验签。

canonical JSON：UTF-8、键递归排序、紧凑分隔符（`,:`）、不允许 NaN/Infinity。

---

## 2. 目录结构

```
app/
  config.py       # 环境变量配置
  numerics.py     # WAD 定点解析与带方向乘除
  crypto.py       # Ed25519 签名/验签、canonical JSON、报告摘要
  engine.py       # 纯函数式回放引擎（无 I/O，全部协议规则）
  models.py       # SQLAlchemy 表（events / reports）
  repository.py   # 仓储：内存实现 + PostgreSQL 实现
  service.py      # 验签、规范化、迟到判定、重算、报告签名
  api.py          # FastAPI 路由与应用工厂
scripts/
  generate_keys.py  sign_events.py  split_batch.py
  verify_report.py  acceptance.sh
examples/           # 24 个主场景事件 + 1 个迟到事件（含未签名/已签名）
tests/              # 34 个测试（33 内存 + 1 PG 集成）
keys/               # 本机生成的演示密钥（.gitignore 忽略私钥）
```

---

## 3. 本地启动

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python scripts/generate_keys.py          # 首次：生成 feeder/server 两套 Ed25519 密钥
```

### 方式 A：内存存储（零外部依赖，最快上手）

```bash
ALLOW_RESET=1 uvicorn app.api:app --host 127.0.0.1 --port 8000
```

### 方式 B：PostgreSQL

```bash
docker compose up -d db                                          # 127.0.0.1:55432
export DATABASE_URL="postgresql+asyncpg://replay:replay@127.0.0.1:55432/lreplay"
uvicorn app.api:app --host 127.0.0.1 --port 8000                # 表自动创建
```

服务启动时如检测到本机有全局代理（`ALL_PROXY` 等），对 `127.0.0.1` 的 curl
请加 `--noproxy '*'` 或 `unset` 代理变量。

### 关键环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `DATABASE_URL` | 空（=内存库） | `postgresql+asyncpg://…` |
| `REQUIRE_SIGNATURES` | `1` | 设 `0` 可关闭事件验签（仅调试） |
| `PRICE_STALENESS_SECONDS` | `60` | 价格陈旧阈值 |
| `MAX_REPAY_NUM/MAX_REPAY_DEN` | `1/2` | 单次清算偿还比例 |
| `ALLOW_RESET` | `0` | 暴露 `POST /admin/reset`（开发/验收用） |
| `FEEDER_PUBLIC_KEY_HEX` / `SERVER_PRIVATE_KEY_HEX` | 文件 | 也可用 env 直接注入密钥 |

---

## 4. HTTP 接口

| 方法/路径 | 说明 |
|---|---|
| `GET /health` | 存储模式、签名开关、陈旧阈值 |
| `POST /events` | 批量提交已签名事件；返回 `accepted/replay_version/late_detected/digest/signature` |
| `GET /events` | 已入库事件（含 seq、late 标记、所属版本） |
| `GET /reports` | 全部报告版本（摘要、链指针） |
| `GET /reports/latest` | 最新版本完整报告 |
| `GET /reports/{version}` | 指定历史版本（**永久保留**） |
| `POST /admin/reset` | 清空重置（仅 `ALLOW_RESET=1`） |

错误：坏签名 400 `bad_signature`；重复 event_id 409 `duplicate_event_id`；
非法定点/缺字段 400 `bad_event`。

---

## 5. 验收命令

### 一键验收（内存库，自动起服务、签名、提交、迟到重算、验签）

```bash
source .venv/bin/activate
./scripts/acceptance.sh
```

对 PostgreSQL 跑同一套验收：

```bash
docker compose up -d db
DATABASE_URL="postgresql+asyncpg://replay:replay@127.0.0.1:55432/lreplay" ./scripts/acceptance.sh
```

### 自动化测试

```bash
pytest -q                                   # 33 个：定点/引擎/密码/服务/HTTP（内存）
DATABASE_URL=postgresql+asyncpg://replay:replay@127.0.0.1:55432/lreplay \
    pytest -q                                   # 34 个：追加真实 PG 持久化/版本报告集成测试
```

### 手动逐步验收

```bash
# 1) 签名示例事件（真实 Ed25519）
python scripts/sign_events.py examples/events_unsigned.jsonl examples/events.signed.jsonl
python scripts/sign_events.py examples/late_event_unsigned.jsonl examples/late_event.signed.jsonl

# 2) 提交主批次（24 事件：利率切换、开仓/存/借、两次价格更新、5 笔清算）
curl -s --noproxy '*' -X POST localhost:8000/events \
  -H 'Content-Type: application/json' \
  --data "$(jq -c '{events: [inputs]}' examples/events.signed.jsonl)" | jq

# 3) 看 v1：alice 被连续清算两次（各 50%），bob 恰好 H=1 拒绝，carol 缺价格，alice 陈旧价格暂停
curl -s --noproxy '*' localhost:8000/reports/latest | jq '.body.result.liquidations, .body.result.skipped'

# 4) 提交迟到事件（ts=159，早于已处理的 230）-> 生成 v2，v1 保留
curl -s --noproxy '*' -X POST localhost:8000/events \
  -H 'Content-Type: application/json' \
  --data "$(jq -c '{events: [inputs]}' examples/late_event.signed.jsonl)" | jq

# 5) 独立验证报告签名 + 哈希链
BASE_URL=http://localhost:8000 python scripts/verify_report.py
```

### 示例场景对照表（`examples/events_unsigned.jsonl`）

| event_id | 验收点 |
|---|---|
| `e-rate-1`(t=0) / `e-rate-2`(t=100) | **利率切换边界**：[0,100) 用 1e-9，[100,230] 用 2e-9 |
| `e-liq-a-1`(t=160) / `e-liq-a-2`(t=170) | **重复清算**：第一次还当前债务 50%，第二次还剩余债务 50%（本金口径 50/25，另含极少累计利息），各按 10% 奖励没收抵押 |
| `e-liq-b-1`(t=200，同秒借 100) | **健康度恰好 1**：抵押 100 USDC = 债务 100，严格 `<1` 才可清算 → `healthy` 拒绝 |
| `e-liq-d-1`(t=160) | H=1.142…，健康 → `healthy` 拒绝 |
| `e-liq-c-1`(t=180) | XYZ **无报价** → `missing_price`，暂停 |
| `e-liq-a-stale`(t=230) | ETH 报价停在 t=150，年龄 80s **> 60s** → `stale_price`，暂停 |
| `e-dep-a-late`(t=159，第二批) | **迟到事件版本化重算**：v1 的两笔清算在 v2 全部变 `healthy`，旧报告保留 |
| 同秒事件（如 t=0 的 12 个事件） | **稳定排序**：严格按入库 `seq`，调换入库顺序结果随之改变（见引擎测试） |

---

## 6. 设计说明

- **回放确定性**：引擎是纯函数，同一份事件集必然得到同一份报告；报告摘要因此可作为复现校验。
- **不做真实交易**：清算只在事件流内改账本（债务/抵押），无任何链上/外部副作用。
- **失败如实报告**：无法执行的清算不会静默丢弃，全部进入报告 `skipped[]` 并带结构化原因
  （`stale_price` 还带资产与价格年龄，`underwater` 情形带 `shortfall_value`）。
- **内存库与 PG 行为一致**：二者实现同一仓储协议；CI/快速测试默认内存库，
  生产与集成测试使用 PostgreSQL（JSONB 存储 payload/报告体）。

## 7. 安全注意

- `keys/` 下为你本机 `generate_keys.py` 生成的演示密钥，已被 `.gitignore` 忽略；
  请勿提交或用于生产。生产请离线生成并通过环境变量注入。
- `ALLOW_RESET=1` 与 `REQUIRE_SIGNATURES=0` 仅限本地调试。
