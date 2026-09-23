# 时间加权价格窗口服务（TWAP Window Service）

纯后端的本地 TWAP 价格服务：接收 **(时间戳, 整数价格, 来源)** 样本，把价格视为
**分段常值函数（piecewise constant）** 对对齐时间窗做积分，输出时间加权均价、
覆盖率与陈旧标志；支持最多 **5 分钟迟到数据** 的增量重算与版本化。

技术栈：**Go 1.22 · go-chi/chi v5 · PostgreSQL 16 · pgx/v5**。无浮点数参与金额
或均价运算（整数积分 + `math/big` 有理值 + 定点小数），写入需 **HMAC-SHA256**
签名并带一次性 nonce 防重放。

---

## 1. 计算语义（重点：不是样本均值）

样本 `s_i = (t_i, p_i, src_i)` 定义价格函数

```
P(t) = p_i   当 t ∈ [t_i, t_{i+1})
```

窗口 `[S, E)` 为 **左闭右开**、按窗口长度对齐（默认 60s，`S = floor(unix/60)*60`）。

窗口的时间加权均价是对分段常值函数的积分：

```
TWAP = ∫_S^E P(t) dt / 覆盖时长（微秒）
```

例如窗口内价格 `100` 持续 50 秒、`200` 持续 10 秒：

- ❌ 样本均值：`(100+200)/2 = 150`
- ✅ 时间加权：`(100·50 + 200·10)/60 = 116.666667`

边界与缺口规则（均有测试覆盖）：

| 情形 | 处理 |
|---|---|
| **窗口起点前的样本（carry-in）** | 取 `t ≤ S` 的最新样本，其价格携带进入窗口 |
| **窗口起点前没有任何样本** | 窗口前缀 **不覆盖**；绝不允许更晚（未来）的样本回填历史 |
| **重复时间戳 / 来源冲突** | 同一 `t` 只允许一个有效价格：按来源 `priority` 高者胜，平局取来源名字典序最大者；同价不同源不算冲突。`priority` 模式记录败者到 `conflicts`；`reject` 模式 HTTP 409 拒绝 |
| **窗口边界样本** | `t == E` 属于下一个窗口；`t == S` 属于本窗口 |
| **未来价格 / asOf** | `t > now` 的样本对当前计算不可见；窗口超出 now 的部分不覆盖，窗口标记 `stale=true` |
| **陈旧上限（stale horizon）** | 携带价格只在样本时间后 `TWAP_STALE_SEC`（默认 120s）内可信，超期部分不覆盖并置陈旧 |
| **不足覆盖** | 返回 `covered_usec`、`window_usec` 与定点 `coverage ∈ [0,1]`；未覆盖部分不参与均值，**不**拿任何价格填充 |
| **迟到数据** | 样本时间早于 `now − 300s` 一律拒绝（HTTP 422）；5 分钟内的迟到/更正被接受，事务内重算所有受影响窗口，内容变化则追加新版本 |

**版本化**：每个窗口版本带一个对“有效事件 + 精确积分结果 + 覆盖率 + 陈旧
+ 冲突 + 语义版本前缀 `v1|`”的 SHA-256 规范哈希（`content_hash`）。重算后哈希
不变就不写新版本；哈希变了版本号 +1。

---

## 2. HTTP 协议

### 认证

- 管理接口：HTTP Basic（用户 `admin`，密码 `TWAP_ADMIN_KEY`）。
- 写入接口：每个请求头

  | Header | 含义 |
  |---|---|
  | `X-Source` | 已注册来源名 |
  | `X-Timestamp-Usec` | 请求时间（微秒 epoch），允许 ±5 分钟时钟偏移 |
  | `X-Nonce` | 一次性随机串（落库去重，重放返回 409） |
  | `X-Signature` | `base64(HMAC_SHA256(secret, ts_us + "\n" + nonce + "\n" + body))` |

  签名覆盖的是 **原始请求体字节**；常量时间比较。

### 端点

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` `/readyz` | 存活 / 数据库就绪 |
| POST | `/v1/admin/sources` | 注册来源 `{name,priority,secret?}`（Basic） |
| POST | `/v1/admin/rebuild` | 全量重建窗口版本（Basic） |
| POST | `/v1/samples` | 写入单条或批量样本（HMAC） |
| GET | `/v1/symbols/{symbol}/windows/{start}` | 读一个对齐窗口（start 为 unix 秒；`?persist=0` 只实时算不落版本） |
| GET | `/v1/symbols/{symbol}/windows?from=&to=` | 列出区间内已物化窗口的最新版本 |

写入体示例见 [`examples/`](examples)：

```json
{ "symbol": "BTC/USD", "ts_us": 1735689600000000, "price": 95000 }
```

批量：`{"samples": [ {…}, {…} ]}`。

读取响应字段：

```jsonc
{
  "symbol": "BTC/USD",
  "window_sec": 60,
  "start_unix": 1735689600,
  "end_unix":   1735689660,
  "twap": "10050.000000",        // 6 位定点；完全未覆盖时为 ""
  "twap_exact": "603000000000/60000000", // 精确有理值 num/den
  "integral": "603000000000",    // 价格·微秒，精确整数
  "covered_usec": 60000000,
  "window_usec":  60000000,
  "coverage": "1.000000",
  "stale": false,
  "sources": ["venueA","venueB"],
  "conflicts": [],
  "version": 4,
  "content_hash": "c8d1…",
  "live": false
}
```

错误状态：401 签名/来源/时钟偏移失败，409 来源冲突或 nonce 重放，
422 超过迟到窗口，400 参数错误。

---

## 3. 目录结构

```
cmd/server/         HTTP 服务入口
cmd/sign/           离线 HMAC 签名小工具（配合 curl）
internal/domain/    核心类型、窗口对齐、配置
internal/twap/      纯计算：分段积分、冲突裁决、定点舍入、规范哈希、受影响窗口
internal/store/     PostgreSQL：schema 嵌入、样本/nonce/版本读写（pgx, 事务）
internal/service/   编排：迟到校验→写样本→增量重算→版本化；全量重建
internal/httpapi/   chi 路由、HMAC 中间件、处理器
internal/auth/      HMAC-SHA256、随机密钥/nonce
examples/           示例请求体
scripts/            setup-db.sh（建库）、demo.sh（端到端验收）
```

---

## 4. 本地启动

前置：Go ≥ 1.22、PostgreSQL（本机已验证 16）、`curl`、`jq`、`openssl`。

```bash
# 1) 建角色与数据库（幂等；需要 sudo 到本机 postgres 超级用户）
bash scripts/setup-db.sh
#   -> postgres://twap:twappw@127.0.0.1:5432/twap?sslmode=disable

# 2) 依赖已锁定（go.mod / go.sum），直接构建运行
go build ./...
go run ./cmd/server \
  -addr 127.0.0.1:8080 \
  -dsn 'postgres://twap:twappw@127.0.0.1:5432/twap?sslmode=disable' \
  -window-sec 60 -late-tolerance-sec 300 -stale-horizon-sec 120 \
  -conflict-mode priority -admin-key 'change-me'
```

可用环境变量：`TWAP_ADDR`、`TWAP_DSN`、`TWAP_WINDOW_SEC`、`TWAP_LATE_SEC`、
`TWAP_STALE_SEC`、`TWAP_FUTURE_GRACE_SEC`、`TWAP_CONFLICT_MODE`、
`TWAP_ADMIN_KEY`。Schema 在启动时自动幂等迁移。

---

## 5. 验收命令

### 一键端到端验收（推荐）

```bash
bash scripts/demo.sh
```

它会真实构建并启动服务、注册 HMAC 来源、写入不等间隔样本，并断言 31 项：
时间加权≠均值、整窗无变化、窗口边界、来源冲突、5 分钟内迟到重算+新版本、
超期 422、错误签名 401、nonce 重放 409、**增量结果与全量重建的哈希逐窗一致**。

### 自动化测试

```bash
# 全部测试（纯逻辑无需数据库；集成测试连真实 PostgreSQL）
go test ./...

# 只跑纯计算（无外部依赖）
go test ./internal/twap/... ./internal/auth/...

# 数据库不在本机时跳过集成测试
TWAP_TEST_SKIP_DB=1 go test ./...

# 指定测试库
TWAP_TEST_DSN='postgres://user:pass@host:5432/db?sslmode=disable' go test ./...
```

测试矩阵覆盖需求点名的全部场景：

- **不等间隔**：`100@0s, 200@50s → 116.666667`，并断言不等于样本均值 150；
- **整窗无变化**：单点样本携带全窗，TWAP 等于该价、覆盖 1.0、不陈旧；
- **窗口边界**：`t=E` 归下一窗，`t=S` 归本窗；
- **来源冲突**：priority 裁决 + 冲突上报；reject 模式 409；同价不算冲突；
- **增量/全量一致**：乱序写入 + 迟到更正后，逐窗比较两种路径的
  `content_hash / twap / coverage / stale`；
- 另有：起点无样本不回填、陈旧上限截断、asOf 截断、微秒级权重、舍入规则、
  HMAC 篡改/重放/时钟偏移、空窗覆盖率与陈旧标志。

### 手动 curl 示例

```bash
# 注册来源（拿到 secret_base64；也可在请求里自带 secret）
curl -u admin:dev-admin-key -H 'Content-Type: application/json' \
  -d '{"name":"venueA","priority":10}' \
  http://127.0.0.1:8080/v1/admin/sources

TS=$(date +%s%6N)            # GNU date 微秒
NONCE=$(openssl rand -hex 16)
BODY='{"symbol":"BTC/USD","ts_us":'$TS',"price":95000}'
SIG=$(printf '%s' "$BODY" | go run ./cmd/sign -secret "$SECRET" -ts-us "$TS" -nonce "$NONCE")

curl -H 'Content-Type: application/json' \
  -H "X-Source: venueA" -H "X-Timestamp-Usec: $TS" \
  -H "X-Nonce: $NONCE" -H "X-Signature: $SIG" \
  -d "$BODY" http://127.0.0.1:8080/v1/samples

START=$(( TS/1000000/60*60 ))
curl "http://127.0.0.1:8080/v1/symbols/BTC%2FUSD/windows/$START"
```

> 注意：签名必须覆盖**与发送字节完全一致**的 body（`curl -d` 默认不追加
> 换行）；命令行里用 `printf '%s' "$BODY" | …` 而不是 here-string。

---

## 6. 设计说明 / 取舍

- **精确运算**：时间用微秒 epoch `BIGINT`；积分与均价全程整数/`big.Rat`，
  落库 `twap_exact` 为 `num/den` 文本、`integral` 为 `NUMERIC` 整数，展示层
  才转 6 位定点（半数远离零舍入）。
- **增量重算范围**：样本 `t` 影响 `[t, min(下一样本, t+陈旧上限, now))`；对
  已存在时间戳的更正还会向后影响 `[max(上一样本, t−陈旧上限), t)`。影响集
  按窗口对齐保守扩展，哈希相同则不落版本。
- **事务一致性**：每次写入在单个事务内完成上下文读取、样本 upsert、受影响
  窗口重算与版本插入；全量重建同样在单事务内清空并重建全部 symbol。
- **不做的事**：不用未来样本回填历史；未覆盖段不做插值、不折算进均价；
  空窗不物化版本（实时读取返回 `coverage=0, stale=true`）。
