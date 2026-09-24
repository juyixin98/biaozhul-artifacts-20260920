# 围栏令牌资源保护（Fencing Token Demo）

纯后端演示：用 Go 标准库 `net/http` 实现 Martin Kleppmann 提出的
**围栏令牌（fencing token）** 模式，本地双组件模拟「锁服务 + 资源服务」。

- **锁服务**：单锁租约。每次授予产生**严格递增**的 fencing token；租约到期后
  新持有者获得更大的 token；令牌计数器持久化，**重启后不回退**。
- **资源服务**：维护「已见最大令牌」高水位线。写请求必须携带令牌，
  `token < 高水位` 一律拒绝（409）；高水位持久化，重启后旧持有者的迟到写仍被拒绝。
- **注入时钟**：`clock.Clock` 接口（`Real` 墙钟 / `Fake` 可控假时钟），
  测试用假时钟精确控制租约过期，无需 sleep；演示脚本用真实墙钟验证同一逻辑。

## 依赖

- Go ≥ 1.22（开发环境使用 go1.23.4）
- **零第三方依赖**：仅用标准库（`net/http`、`encoding/json` 等），
  `go.mod` 无 require，`go.sum` 为空。
- 演示脚本 `scripts/demo.sh` 额外需要 `curl` 和 `jq`（仅用于发请求/解析输出）。

## 启动

```bash
go build -o fencingdemo ./cmd/fencingdemo

# 一个进程内启动两个独立 HTTP 服务（本地双组件模拟）
./fencingdemo \
  -lock-addr :8080 \        # 锁服务地址（默认 :8080）
  -resource-addr :8090 \    # 资源服务地址（默认 :8090）
  -data-dir ./data \        # 快照目录（默认 ./data）
  -ttl 10s                  # 锁租约 TTL（默认 10s）
```

也可用环境变量：`LOCK_ADDR`、`RESOURCE_ADDR`、`DATA_DIR`、`LOCK_TTL`。
Ctrl+C 触发优雅关闭。两个组件无内存共享，仅共享数据目录。

## HTTP 接口

### 锁服务（默认 :8080）

| 方法 | 路径 | 请求体 | 响应 |
|---|---|---|---|
| POST | `/lock/acquire` | `{"lease_id":"A"}` | 200 `{"lease_id":"A","fencing_token":1,"issued_at":"...","expires_at":"...","ttl_ms":10000}`；锁被占用时 409 |
| POST | `/lock/renew` | `{"lease_id":"A"}` | 200 续期后的租约；非持有者 403；已过期 410 |
| POST | `/lock/release` | `{"lease_id":"A"}` | 200 `{"status":"released"}`；非持有者 403；已过期 410 |
| GET | `/lock/status` | — | `{"held":true,"lease":{...},"next_token":2,"ttl_ms":10000}` |
| GET | `/healthz` | — | `{"status":"ok"}` |

### 资源服务（默认 :8090）

| 方法 | 路径 | 请求体 | 响应 |
|---|---|---|---|
| POST | `/resource/write` | `{"fencing_token":1,"lease_id":"A","value":"x"}` | 接受：200 `{"accepted":true,"version":1,"high_water_mark":1,"observed_token":1}`；令牌过期：409 `{"accepted":false,"high_water_mark":2,"observed_token":1}` |
| GET | `/resource/read` | — | `{"value":"x","version":1,"high_water_mark":1}` |
| GET | `/healthz` | — | `{"status":"ok"}` |

### 请求样例（curl）

```bash
# 1. A 获取锁，拿到令牌 1
curl -s -X POST localhost:8080/lock/acquire -d '{"lease_id":"A"}'
# {"lease_id":"A","fencing_token":1,...}

# 2. A 携带令牌写资源 —— 接受
curl -s -X POST localhost:8090/resource/write \
  -d '{"fencing_token":1,"lease_id":"A","value":"write-by-A"}'
# {"accepted":true,"version":1,"high_water_mark":1,"observed_token":1}

# 3. （A 暂停超过 TTL 后）B 获取锁，拿到令牌 2
curl -s -X POST localhost:8080/lock/acquire -d '{"lease_id":"B"}'
# {"lease_id":"B","fencing_token":2,...}

# 4. B 写入 —— 接受，高水位升为 2
curl -s -X POST localhost:8090/resource/write \
  -d '{"fencing_token":2,"lease_id":"B","value":"write-by-B"}'

# 5. A 恢复后用旧令牌 1 重试 —— 被拒绝
curl -s -X POST localhost:8090/resource/write \
  -d '{"fencing_token":1,"lease_id":"A","value":"stale-write-by-A"}'
# HTTP 409 {"accepted":false,"version":0,"high_water_mark":2,"observed_token":1}
```

## 测试

```bash
go test -race -count=1 ./...
```

覆盖：

- `internal/lock`：令牌递增、互斥、续期/释放语义、过期错误、**重启后令牌不回退**（持久化单测）
- `internal/resource`：高水位拒绝旧令牌、等令牌重试、重启后高水位保留
- `internal/e2etest`：黑盒 HTTP 端到端验收——
  `TestFencingAcceptanceScenario` 完整复现「暂停旧持有者 → 租约过期 →
  新持有者写入 → 旧写被拒 → 双服务重启后令牌不回退、旧写仍被拒」；
  `TestConcurrentAcquiresOnlyOneWins` 并发抢锁仅一人获胜；
  `TestWallClockEndToEnd` 用真实墙钟跑同一过期场景。

## 端到端演示

```bash
bash scripts/demo.sh
```

脚本自动构建二进制、以 TTL=3s 启动两个服务，用 curl 走完整验收流程
（A 写 → A 暂停至过期 → B 写 → A 旧令牌重试被拒 → 重启双服务 →
令牌不回退 → C 获取令牌 3 写入），最后输出 PASS/FAIL 汇总。

## 实测结果（2026-09-24，go1.23.4 linux/amd64）

- `go test -race -count=1 ./...`：**全部通过**
  （`internal/e2etest`、`internal/lock`、`internal/resource` 三个包 ok，
  其余包无测试文件）
- `bash scripts/demo.sh`：**13/13 PASS**。关键输出摘录：

```
== 6. A resumes and retries its write carrying STALE token 1 ==
  HTTP 409  {"accepted":false,"version":0,"high_water_mark":2,"observed_token":1}
  PASS: A's stale write rejected (409, accepted=false)
== 7. Restarting both services ==
  lock status: ... "next_token":3 ...
  PASS: lock next_token survived restart with no rollback (want 3, got 3)
  A retries after restart: HTTP 409
  PASS: A's stale token still rejected after restart
== Summary: 13 passed, 0 failed ==
```

## 设计说明与取舍

- **持久化**：每次状态变更将快照原子写入（临时文件 + rename + fsync）：
  锁服务存 `lock.json`（`next_token` + 当前租约），资源服务存
  `resource.json`（值、版本、高水位）。重启时加载；已过期的租约被丢弃，
  但令牌计数器保留——这是「重启后令牌不回退」的关键。
- **等号放行**：资源接受 `token >= 高水位`。正确工作的锁不会把同一令牌发给
  两个持有者，等号用于支持合法持有者的**重试**（写操作幂等语义由版本号体现）。
- **资源不回调锁服务**：资源只比较令牌数值，不验证令牌归属——这正是 fencing
  token 模式的要点：即使锁服务与资源之间网络分区，资源也能独立拒绝过期写。
- **单锁**：锁服务只保护一把锁（演示足够）；多锁需按 key 分桶存储。
- **时钟**：生产路径用墙钟（`clock.Real`）；测试注入 `clock.Fake` 精确推进
  时间，避免 sleep 导致的 flaky。

## 未完成项 / 已知限制

- 单进程内模拟两个组件（题目要求本地模拟）；未做跨机器部署、TLS、认证。
- 锁服务为单锁、单副本；无锁服务自身的高可用（真实场景需共识协议，
  如 etcd/ZooKeeper 的 epoch）。
- 持久化为 JSON 文件快照，无崩溃中途的部分写恢复测试（已用原子 rename
  降低风险）；无磁盘满等故障注入测试。
- 资源只有一个键（演示用单值存储），未实现多 key。
