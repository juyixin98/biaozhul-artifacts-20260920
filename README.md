# TWAP — 时间加权价格窗口服务

纯后端的本地 TWAP（time-weighted average price）服务。样本是
`(timestamp, integer price, source)` 三元组；价格被建模为时间的
**右连续分段常值（阶跃）函数**，窗口 TWAP 是该阶跃函数在半开区间
`[window_start, window_end)` 上的**时间积分 / 覆盖时长**——
**不是样本价格的算术平均**。

- 语言/框架：Go 1.22 + [chi](https://github.com/go-chi/chi) v5 路由器
- 存储：PostgreSQL（原始样本 + 只追加的窗口版本）
- 协议：JSON over HTTP
- 密码操作真实执行：SHA-256 输入指纹、HMAC-SHA256 版本签名与验签、
  `crypto/rand` 密钥

## 目录

- [1. 计算语义](#1-计算语义)
- [2. 迟到数据与版本协议](#2-迟到数据与版本协议)
- [3. API](#3-api)
- [4. 本地启动](#4-本地启动)
- [5. 验收命令](#5-验收命令)
- [6. 测试](#6-测试)
- [7. 配置项](#7-配置项)
- [8. 项目结构](#8-项目结构)

---

## 1. 计算语义

样本 `(t, p, source)` 断言：**从 `t` 起，直到同一来源的下一个样本，
价格为 `p`**（forward-fill）。它只对未来做断言，不对过去做断言。

窗口 `W = [wStart, wEnd)` 的结果由以下规则产生：

1. **参与样本**：每个来源取 `ts <= wStart` 的最新样本作为
   **起点锚点（anchor）**；窗内取 `wStart < ts < wEnd` 的全部样本。
   `ts == wEnd` 的样本**不**参与（窗口半开，右端点不属于本窗口）。
2. **分段积分**：以 `wStart` 及每个窗内不同时间戳为分割点。某来源在
   分割点上的值 = 该来源 `ts <= 分割点` 的最新样本。区间
   `[cp, 下一个cp)` 上价格恒定，贡献 `price * 时长`。
3. **起点前样本**：窗口开始前的样本作为 anchor，其 forward-fill
   断言在窗口开始时仍然成立，因此**覆盖整个窗口**（已在
   `TestPreWindowAnchorCoversWholeWindow` 中锁定）。窗口第一个样本
   之前、且没有 anchor 的前缀是**未覆盖**的，不允许用任何价格回填。
4. **不足覆盖**：覆盖率 = 覆盖微秒数 / 窗口微秒数，随响应返回。
   TWAP 只在**已覆盖时长**上定义；覆盖率为 0 时 `twap` 为 `null`。
   未覆盖前缀不会被静默当作 0 价或用最近价外推。
5. **重复时间戳**：
   - 同一 `(ts, source)` 多行 → upsert，**最后一行生效**，不会
     被重复计权；
   - 不同来源同一 `ts` 全部生效（见来源冲突）。
6. **来源冲突**：同一时刻两个来源价格不同 → 该时段 `conflict=true`，
   计入 `conflict_count`。获胜方采用**确定性**策略：来源名字典序
   最小者（文档化、可复现，不依赖插入顺序）。
7. **未来价格**：`ts > now` 的样本在入库前被拒绝（HTTP 422）；
   开放窗口实时计算时只使用 `ts < now` 的数据。**未来价格永不填补
   历史**。开放窗口的覆盖分母仍是整窗长度，所以数据稀疏时覆盖率
   小于 1，`stale=true`。
8. **精确算术**：积分与 TWAP 用 `math/big` 整数/有理数计算，对外
   同时给出 `twap_exact`（约分后的分子/分母）和浮点 `twap`。

### 示例（不等间隔，非算术平均）

窗口 60s，样本 `(0, 100, s)`、`(10s, 200, s)`：

- 算术平均（错误做法）= 150
- 时间加权（本服务）= `(100*10 + 200*50) / 60 = 550/3 ≈ 183.333`

整窗无变化时 TWAP 就是该常值，覆盖率 1，且只有一个分段。

## 2. 迟到数据与版本协议

固定窗口为 epoch 对齐的 `WINDOW` 桶（默认 60s）。桶
`[bStart, bEnd)` 的生命周期：

| 状态 | 条件 | 行为 |
|---|---|---|
| `open` | `now < bEnd` | 不物化；读取时用当前数据实时计算（无未来数据） |
| `closed` | `bEnd <= now < bEnd + MAX_LATE` | 已物化；迟到样本可重算并追加新版本 |
| `frozen` | `now >= bEnd + MAX_LATE` | 拒绝迟到样本（422）；最后版本永久有效 |

- `MAX_LATE` 默认 **5 分钟**。样本只可能影响两个桶：它自己所属的桶
  （窗内变价点）和紧随其后的桶（起点 anchor）。
- 每次摄入在**按窗口咨询锁**（`pg_advisory_xact_lock`）保护的事务里
  重算受影响的 closed 桶：加载原始样本 → 内核计算 → 对**规范化输入**
  求 SHA-256 `input_hash` → 与最新版本比较，**仅当哈希变化**才追加
  `version+1`。幂等重放不产生新版本。
- 版本只追加、不可变；`GET /windows/{start}/versions/{n}` 可读取任一
  历史版本。
- 每个版本携带对规范版本载荷的 **HMAC-SHA256 签名**。读取时带
  `?verify_signature=1` 会用配置的 `SIGNING_KEY` 重新验签并返回
  `signature_ok`。直接篡改数据库行（例如改 `twap_num`）会使验签失败
  （见 `TestTamperedStoredSignatureFails`）。
- **增量/全量一致**：摄入路径（增量）与 `POST /v1/admin/recompute`
  （全量）调用**同一个** `computeVersion`：同一份样本、同一个内核、
  同一个哈希与签名。全量扫描对每个 closed 桶追加的新版本数为 0，
  除非存在此前从未被增量路径覆盖过的桶（例如仅作为别人 anchor
  出现的桶）；全量补齐后再次扫描必然为 0。

`input_hash` 的规范化编码对样本按 `(ts, source)` 排序、逐字段加标签，
来源字符串用 `strconv.QuoteToASCII` 转义，使来源中的换行/制表符无法
伪造另一组输入的摘要。

## 3. API

所有时间戳接受 **Unix 微秒整数** 或 **RFC3339Nano 字符串**
（如 `"2026-09-22T12:00:00Z"`）。价格为整数。

### `POST /v1/samples` — 摄入（单条或批量）

```json
{
  "batch": [
    {"ts": 1790085660000000, "price": 100, "source": "exchange-a"},
    {"ts": "2026-09-22T12:00:05Z", "price": 102, "source": "exchange-b"}
  ]
}
```

也接受不带 `batch` 的单条对象。响应逐项给出状态：

```json
{
  "accepted": 1,
  "rejected": 1,
  "items": [
    {"index": 0, "status": 200, "result": {"changed": true, "recomputed_windows": [...]}},
    {"index": 1, "status": 422, "error": "sample timestamp is in the future..."}
  ]
}
```

- 422：未来样本；超过 5 分钟迟到上限的样本。
- 批量端点整体返回 200，**逐项**判定，错误信息在 item 中。

### `GET /v1/windows/latest?at=<ts>&verify_signature=1`

返回 `at` 所在固定窗口的最新结果（closed/frozen 读存储版本，
open 实时计算）。字段：

```json
{
  "window_start": 1790085660000000,
  "window_end":   1790085720000000,
  "state": "closed",
  "version": 4,
  "input_hash": "4d0bda…",
  "signature": "9120cb…",
  "signature_ok": true,
  "twap": 170,
  "twap_exact": "170/1",
  "coverage": 1,
  "coverage_exact": "60000000/60000000",
  "covered_micros": 60000000,
  "window_micros": 60000000,
  "stale": false,
  "conflict_count": 3,
  "samples_used": 4,
  "has_anchor": true,
  "last_sample_ts": 1790085690000000,
  "segments": [ {"start": ..., "end": ..., "price": 100, "covered": true,
                 "conflict": true, "sources": 2} ],
  "live": false
}
```

- `coverage`：覆盖率（浮点），`coverage_exact`：精确分数。
- `stale`：窗口有效末端（open 时为 `now`）上携带价格的最新样本，
  年龄超过 `STALE_AFTER`（默认 30s）即为陈旧；完全无数据也为陈旧。
- `segments`：窗口的常值分段，可直接审计每段的价格/冲突/来源数。
- open 窗口：`live=true`、`version=0`、不提供签名（数据仍在变动）。

### `GET /v1/windows/{window_start}/versions/{n}`

读取某窗口的第 `n` 个不可变历史版本。

### `GET /v1/range?start=<ts>&end=<ts>`

任意半开区间的实时 TWAP（原始扫描的权威参考实现，用于与增量物化
结果交叉核对）。

### `POST /v1/admin/recompute`

全量重算（需管理员令牌）：

```
Authorization: Bearer $ADMIN_TOKEN
```

返回 `checked_windows`、`new_versions` 与每个变化窗口。

### 其他

- `GET /healthz`
- `GET /v1/windows?limit=50`：已有物化版本的窗口起点列表。

## 4. 本地启动

需要 Go ≥ 1.22 和一个可连接的 PostgreSQL（本仓库在 Go 1.22 下使用
`chi v5.2.5`、`pgx/v5 v5.6.0`，依赖已在 `go.sum` 锁定）。

### 方式 A：本机 PostgreSQL（本仓库开发所用）

```bash
# 1) 建库建角色（已有 postgres 超级用户时）
sudo -u postgres psql -c "CREATE ROLE twap LOGIN PASSWORD 'twap';"
sudo -u postgres psql -c "CREATE DATABASE twap OWNER twap;"

# 2) 构建并启动（schema 在启动时自动创建/幂等迁移）
go build -o bin/twapd ./cmd/twapd
DATABASE_URL='postgres://twap:twap@localhost:5432/twap?sslmode=disable' \
SIGNING_KEY=$(openssl rand -hex 32) \
ADMIN_TOKEN=dev-admin-token \
./bin/twapd
```

### 方式 B：docker-compose 提供 PostgreSQL

```bash
docker compose up -d postgres
DATABASE_URL='postgres://twap:twap@localhost:55432/twap?sslmode=disable' \
SIGNING_KEY=$(openssl rand -hex 32) \
ADMIN_TOKEN=dev-admin-token \
go run ./cmd/twapd
```

不设置 `SIGNING_KEY` 时，进程启动会用 `crypto/rand` 生成一把临时
HMAC 密钥并打印（重启后旧版本签名将无法验证——生产请固定密钥）。

快速冒烟：

```bash
curl -s localhost:8080/healthz
BASE=http://localhost:8080 bash examples/curl_example.sh
```

## 5. 验收命令

```bash
# 全部自动化测试（纯内核/密码学测试不需要数据库；集成测试需要 PG，
# 连不上会 t.Skip；可用 TEST_DATABASE_URL 覆盖连接串）
go test ./... -count=1
go test -race ./... -count=1

# 端到端验收（先在 :18091 起一个服务）
SIGNING_KEY=4141…41 ADMIN_TOKEN=acceptance-admin-token \
  LISTEN_ADDR=127.0.0.1:18091 ./bin/twapd &
BASE=http://127.0.0.1:18091 ADMIN_TOKEN=acceptance-admin-token \
  bash scripts/acceptance.sh
```

`scripts/acceptance.sh` 对真实服务、真实时钟执行 17 项断言，覆盖：
不等间隔 TWAP、来源冲突与确定性获胜、覆盖率、迟到修正重算与版本递增、
历史版本不可变、不足覆盖与 stale 标志、未来样本与超期样本被拒、
管理员鉴权、以及全量重算幂等（增量/全量收敛）。

## 6. 测试

| 包 | 内容 |
|---|---|
| `internal/twap` | 13 个纯内核用例：不等间隔（非算术平均）、整窗无变化、半开边界、起点前 anchor、零/不足覆盖、重复时间戳 upsert、来源冲突（全程/中途消除）、未来价格隔离、stale、有理约数、非法窗口 |
| `internal/crypto` | SHA-256 确定性/顺序无关/内容敏感、HMAC 签名验签与篡改/错钥/畸形签名拒绝、随机密钥、规范化编码抗来源注入 |
| `internal/service` | 真实 PostgreSQL 集成：迟到重算版本化、幂等不生版本、冻结拒绝、未来拒绝、**增量==全量==原始区间扫描**、迟到 anchor 影响下一窗口、不足覆盖/stale、库内篡改验签失败、20 并发摄入 |
| `internal/httpapi` | chi 端到端：批量逐项状态、微秒/RFC3339 双格式、管理员鉴权、参数校验、404 |

## 7. 配置项

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `DATABASE_URL` | `postgres://twap_p015_a:…@localhost:5432/twap_p015_a?sslmode=disable` | PostgreSQL 连接串 |
| `LISTEN_ADDR` | `:8080` | HTTP 监听地址 |
| `WINDOW` | `60s` | 固定窗口长度，Go duration（如 `60s`）或微秒整数 |
| `MAX_LATE` | `5m` | 迟到上限，超过则拒绝样本并冻结窗口 |
| `STALE_AFTER` | `30s` | 窗口末端携带价格的样本超过该年龄则 `stale=true` |
| `SIGNING_KEY` | 启动时随机生成 | HMAC 密钥的 hex 编码（≥16 字节，建议 32） |
| `ADMIN_TOKEN` | 空（禁用全量重算端点） | `/v1/admin/recompute` 的 Bearer 令牌 |

## 8. 项目结构

```
cmd/twapd/                进程入口：配置、迁移、HTTP 启动
internal/twap/            纯计算内核（分段常值积分，math/big）
internal/crypto/          SHA-256 输入指纹 + HMAC-SHA256 签名/验签
internal/storage/         PostgreSQL schema 与 SQL（咨询锁、版本表）
internal/service/         摄入/迟到/冻结/版本化协议，增量与全量同源
internal/httpapi/         chi 路由与 JSON 协议
examples/                 curl 示例
scripts/acceptance.sh     端到端验收脚本
docker-compose.yml        本地 PostgreSQL
```
