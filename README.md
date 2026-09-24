# 渐进发布判定器（Progressive Rollout Decision Engine）

本地的渐进发布**纯后端**决策服务：Go + [Chi](https://github.com/go-chi/chi/v5) + PostgreSQL。
错误率 / 延迟指标来自一个确定性的**桩服务（stub）**，判定器对真实拉取到的原始样本做聚合与
**置信区间**计算，再决定 5% → 20% → 50% → 100% 各阶段是否可以推进。

没有"指标看着还行就算健康"这种事：**指标缺失、窗口没走完、样本不足一律判 `unknown`，绝不放行**。

---

## 1. 核心语义

### 阶段权重（固定四档）

| 阶段 | 流量权重 | 推进条件 |
|------|----------|----------|
| `5%`  | 0.05 | 完成本阶段观察窗口 + 样本达标 + 区间证据健康 |
| `20%` | 0.20 | 同上 |
| `50%` | 0.50 | 同上 |
| `100%`| 1.00 | 同上；在 100% 再执行一次 `advance` 即 `complete` |

### 推进的两个硬门槛

1. **完整观察窗口**：自进入当前阶段起，必须经过 `observation_ms`（默认 2000ms，必须是
   200ms 桶宽的整数倍），且桩返回的桶必须**铺满整个窗口**（`buckets_covered == buckets_required`）。
2. **最小样本量**：窗口内的请求总数必须 ≥ `min_samples`（默认 80）。

两者任一不满足，判定为 `unknown`，`advance` 被拒绝。

### 三态判定（不是布尔健康/不健康）

- `healthy`：窗口完整、样本充足，且两个 95% 置信区间的**上界**都在阈值内 → 允许 `advance`。
- `degraded`：窗口完整、样本充足，但至少一个区间上界越界 → 禁止 `advance`，只允许 `pause`/`rollback`。
- `unknown`：窗口未满 / 覆盖不全 / 样本不足 / 指标缺失（503 黑窗）→ 不放行。

### 区间证据（每个判定都附带，且是真实计算，不是查表）

- **错误率**：Wilson score 区间（在错误率接近 0 时仍然有效，不会出现负的下界）。
  比较的是 **Wilson 上界** vs `error_rate_upper`。
- **延迟**：对窗口内的逐请求延迟样本计算均值的 **Student-t 95% 区间**（t 分位数由不完全
  Beta 函数反演、二分求解；正态逆 CDF 用 Acklam 有理逼近）。比较的是 **均值上界** vs `latency_mean_ms`。
- 用区间上界（保守）而不是点估计：例如 50/1000 = 5% 恰好压线时，Wilson 上界 ≈ 6.5%，仍判退化。

### 阈值版本在发布开始时冻结

- 阈值策略是版本化的（`/api/thresholds`）。
- 创建发布时把**当时最新版本号和完整 spec 快照**写入发布行；之后全局策略再怎么改，
  这条发布的后续判定始终用它启动时冻结的那份（`threshold_version` + `threshold_snapshot`）。
- 新建发布才会拿到最新版本。

### 命令、预期代次与"重试不重复执行"

- 命令：`advance` / `pause` / `resume` / `rollback`。
- 每条写命令必须带 `expected_generation`。服务端在事务内 `SELECT ... FOR UPDATE` 锁住发布行，
  代次不匹配直接 `409 Conflict`，**命令不会执行第二次**。
- 成功推进 / 暂停 / 恢复 / 回退都会 `generation += 1`；被拒绝的命令不改变代次。
- `rollback` 落到上一阶段（首阶段则回退到 0%，状态 `rolled_back`）。回退后发布为终态，
  即便随后指标恢复健康（"迟到的成功"），该健康判定只作为 `late` 证据记录，**不能复活发布**。

### 密码学操作（全部用标准库真实执行）

- **HMAC-SHA256 请求签名**：所有写操作必须携带
  `X-Rollout-Signature: kid="...",ts=<unix>,nonce="<32hex>",sig="<hex>"`。
  签名内容为 `METHOD\nPATH\nTIMESTAMP\nNONCE\nSHA256(body)`；服务端用 `crypto/hmac` 重算并
  `subtle.ConstantTimeCompare` 常量时间比对。
- **一次性 nonce**：每次签名生成 128 位随机 nonce；相同签名的重放（即使在 ±5 分钟时间窗内）
  命中 `used_nonces` 唯一约束 → `401 replay detected`。
- **时间窗**：签名时间戳偏差超过 ±5 分钟拒绝。
- **幂等键**：`Idempotency-Key` 的 SHA-256 摘要入库（不存明文）；同键重试返回首次响应。
  幂等键按 `METHOD:PATH:KEY` 命名空间隔离。

---

## 2. 目录结构

```
.
├── cmd/
│   ├── server/       # 决策服务主程序（Chi + PostgreSQL）
│   ├── stub/         # 确定性指标桩服务（独立可运行）
│   └── acceptance/   # 端到端验收程序（真实 HTTP，18 项断言）
├── internal/
│   ├── stats/        # 正态逆CDF / Wilson区间 / Student-t区间（真实数值计算）
│   ├── crypto/       # HMAC-SHA256 签名、验签、nonce、常量时间比较
│   ├── models/       # 领域模型（阶段、命令、状态、判定）
│   ├── metrics/      # 指标 HTTP 客户端（真实调用桩）
│   ├── metricstub/   # 场景驱动的桩：healthy/spike/spike-short/degraded/insufficient/blackout/late
│   ├── eval/         # 判定引擎 + 命令服务（代次、冻结阈值、回退/迟到证据）
│   ├── store/        # PostgreSQL 实现 + 内存实现（测试用）+ 建表迁移
│   ├── api/          # Chi 路由、HMAC 中间件、幂等中间件、处理器
│   ├── config/       # 环境变量配置
│   └── signclient/   # Go 签名客户端（验收程序复用）
├── examples/         # 示例请求体
├── scripts/          # dev.sh / test.sh / sign-curl.sh
├── Dockerfile
└── docker-compose.yml
```

---

## 3. 本地启动

### 方式 A：Docker Compose（最省事）

```bash
docker compose up --build
# 决策服务  http://localhost:8080
# 指标桩    http://localhost:8081
# PostgreSQL localhost:5432 (rollout/rollout/rollout)
```

宿主机端口被占用时可覆盖任一端口：

```bash
PG_PORT=55444 STUB_PORT=18081 HTTP_PORT=18080 docker compose up --build
```

> 依赖已 **vendor** 到 `./vendor`（`go.mod` / `go.sum` 锁定，`go mod verify` 可校验），
> 运行时镜像用静态二进制 + `scratch`，**整个镜像构建不访问任何 Go 代理或 apk 源**，
> 离线环境也能 `docker compose up --build`。

### 方式 B：本机跑 Go 进程 + Docker 里的 Postgres

```bash
# 终端 0：起 Postgres + stub + server，Ctrl-C 自动清理
./scripts/dev.sh

# 自定义端口：PG_PORT=55444 STUB_PORT=18081 HTTP_PORT=18080 ./scripts/dev.sh
```

### 方式 C：完全手动

```bash
# 1) Postgres
docker run -d --name rollout-pg -e POSTGRES_USER=rollout -e POSTGRES_PASSWORD=rollout \
  -e POSTGRES_DB=rollout -p 5432:5432 postgres:16-alpine

# 2) 桩服务
go run ./cmd/stub -addr :8081

# 3) 决策服务（另一个终端）
DATABASE_URL='postgres://rollout:rollout@localhost:5432/rollout?sslmode=disable' \
STUB_URL=http://localhost:8081 \
go run ./cmd/server
```

服务启动时自动执行建表迁移并播种 v1 默认阈值
（错误率上界 5%、延迟均值上界 120ms、p95 200ms）。

### 配置项（环境变量，括号内为默认值）

| 变量 | 默认 | 说明 |
|------|------|------|
| `HTTP_ADDR` | `:8080` | 决策服务监听地址 |
| `DATABASE_URL` | `postgres://rollout:rollout@localhost:5432/rollout?sslmode=disable` | PG 连接串 |
| `STUB_URL` | `http://localhost:8081` | 指标桩地址 |
| `HMAC_KEY_ID` / `HMAC_SECRET` | `local-dev-key` / `dev-shared-secret-change-me` | 签名密钥（生产请改） |
| `OBSERVATION_MS` | `2000` | 默认观察窗口（须为 200 的整数倍） |
| `MIN_SAMPLES` | `80` | 每阶段最小样本 |

---

## 4. HTTP 协议

读接口无需签名；**所有写接口都要 HMAC 签名**。最方便的是用自带的签名助手：

```bash
# 创建发布（助手内部用 openssl 真实计算 HMAC，与 Go 服务端逐字节一致）
./scripts/sign-curl.sh POST /api/releases @examples/create_release.healthy.json

# 观察当前判定（只读，无需签名）
curl -s http://localhost:8080/api/releases/<id>/evaluate | jq

# 等一个观察窗口后，带"预期代次 0"推进
sleep 2.2
./scripts/sign-curl.sh POST /api/releases/<id>/commands/advance '{"expected_generation":0}'
```

### 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| GET  | `/healthz` | 存活探针 |
| GET  | `/api/thresholds` | 列出所有阈值版本 |
| GET  | `/api/thresholds/latest` | 当前最新版本 |
| POST | `/api/thresholds` | 发布新阈值版本（签名） |
| POST | `/api/releases` | 创建发布并冻结当前阈值（签名） |
| GET  | `/api/releases` | 列出发布 |
| GET  | `/api/releases/{id}` | 发布详情（含冻结快照、当前代次） |
| GET  | `/api/releases/{id}/evaluate` | 对当前阶段做一次判定（只读，留证） |
| POST | `/api/releases/{id}/commands/{advance\|pause\|resume\|rollback}` | 执行命令（签名，带 `expected_generation`） |
| GET  | `/api/releases/{id}/events` | 追加式审计事件 |
| GET  | `/api/releases/{id}/observations` | 历次判定及区间证据 |

### 创建发布请求体

```json
{
  "name": "checkout-api",
  "version": "2026.09.23",
  "scenario": "healthy",        // 选择桩场景，见下
  "observation_ms": 2000,       // 可选，缺省用服务端默认
  "min_samples": 80             // 可选
}
```

### 判定响应（节选）

```json
{
  "frozen_threshold_version": 1,
  "frozen_threshold_snapshot": {"error_rate_upper":0.05,"latency_mean_ms":120,"latency_p95_ms":200},
  "observation": {
    "Stage": "5%", "Generation": 0,
    "Verdict": {
      "health": "healthy",
      "reasons": ["healthy"],
      "allowed_commands": ["advance","pause","rollback"],
      "threshold_version": 1,
      "metrics": {
        "samples": 1100, "errors": 0,
        "buckets_covered": 10, "buckets_required": 10,
        "error_rate_point": 0,
        "error_rate_lower": 0, "error_rate_upper": 0.0034,
        "latency_mean_ms": 49.9, "latency_upper_ms": 51.6, "latency_lower_ms": 48.2,
        "window_start": "...", "window_end": "..."
      }
    }
  }
}
```

推进成功返回 `200 {"applied":true,"release":{...},"verdict":{...}}`；
判定不允许时返回 `409 {"applied":false,"rejection":{"verdict":{...}}}`；
代次过期返回 `409 conflict: ...`。

---

## 5. 受控指标场景（桩服务）

| 场景 | 行为 | 预期判定 |
|------|------|----------|
| `healthy` | 0 错误，延迟均值 ~50ms | `healthy` |
| `spike-short` | 阶段开始即出现 ~1.2s 短尖峰（~5.5% 错误、~170ms） | 短窗口内 `degraded`（短暂尖峰） |
| `spike` | 尖峰出现在 1–3s | 尖峰被长窗口稀释后可回到 `healthy` |
| `degraded` | 全程 ~45% 错误、~250ms，不自愈 | `degraded`，挡住推进 |
| `insufficient` | 每桶仅 3 个请求 | 窗口完整但样本不足 → `unknown` |
| `blackout` | 指标管线 503，无任何数据 | `unknown / metrics_missing`（**不是**健康） |
| `late` | 同 healthy | 用于演示回退后的迟到健康 |

---

## 6. 自动化测试与验收命令

### 单元 / 集成测试（无需数据库也能跑）

```bash
go vet ./...
go test ./...
```

PostgreSQL 集成测试（迁移、行锁事务、nonce 去重、幂等缓存、jsonb 回读）在提供
`ROLLOUT_TEST_DATABASE_URL` 时自动启用，否则跳过：

```bash
docker run -d --name rollout-test-pg -e POSTGRES_USER=rollout -e POSTGRES_PASSWORD=rollout \
  -e POSTGRES_DB=rollout -p 55444:5432 postgres:16-alpine
ROLLOUT_TEST_DATABASE_URL='postgres://rollout:rollout@localhost:55444/rollout?sslmode=disable' \
  go test ./internal/store/ -v
```

### 端到端验收（真实 HTTP，覆盖全部要求）

先按第 3 节起好服务，然后：

```bash
go run ./cmd/acceptance -url http://localhost:8080
# 或一把梭：./scripts/test.sh http://localhost:8080
```

验收程序对**正在运行**的系统执行 18 项断言，逐条打印区间证据，任一失败则以非零码退出：

1. 窗口未满 → `unknown`（不臆断健康）；窗口满 + 健康 → 5%→20% 推进
2. 短暂尖峰（`spike-short`）→ 短窗口 `degraded`，给出越界的区间上界
3. 持续退化（`degraded`）→ `advance` 409 拒绝、`rollback` 成功
4. 样本不足（`insufficient`）→ `unknown/samples_insufficient`，推进被拒
5. 指标黑窗（`blackout`）→ `unknown/metrics_missing`
6. 回退后迟到健康 → 命令被拒且判定带 `late=true`，发布保持 `rolled_back`
7. 阈值在开始时冻结；全局改策略不影响在跑发布；陈旧代次重试 409、不重复执行

---

## 7. 设计取舍

- **用区间上界而非点估计决定健康**，把"样本量"和"不确定性"显式纳入门禁；样本越多区间越窄，
  这也是最小样本门槛之外的第二层统计保护。
- **判定与命令在同一个加了行锁的事务里完成**：先 `FOR UPDATE` 锁行、再拉指标、再决定、再改代次，
  并发命令不会基于过期状态推进。指标 HTTP 调用在事务内（本地窗口级延迟，可接受），换取强一致。
- **`unknown` 是显式一等状态**，和 `degraded` 区分开：前者表示"我们不知道"（只允许暂停/回退），
  后者表示"我们确定它坏了"。
- **桩无状态、可重启**：场景数据由 `created_at` 锚定，`(release, window)` 的响应只取决于窗口本身，
  便于重复验收。
- 单元测试用内存存储跑全流程语义；真实 SQL / 事务 / 唯一约束行为由独立的 PG 集成测试保证。

依赖在 `go.mod` / `go.sum` 中锁定：`chi/v5 v5.1.0`、`pgx/v5 v5.7.2`。
