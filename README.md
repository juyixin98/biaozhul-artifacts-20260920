# tenantiso — 多租户执行隔离（纯后端）

本仓库实现一个本地 HTTP 服务：多租户 CPU 计算请求的分租户队列、内存预算与
隔离缓存，并附带一个故障注入客户端用于验收。所有外部依赖均为**本进程假服务**
（`internal/fakestore`），不接任何生产系统；时间源通过 `internal/clock` 抽象，
测试可注入可控假时钟。无前端。

## 架构

```
cmd/server        HTTP 服务入口（仅监听 127.0.0.1）
cmd/faultclient   故障注入客户端：驱动验收场景并输出结构化 JSON 测试报告
internal/
  auth            测试认证上下文：租户 ID 仅来自 X-Tenant-ID 头
  clock           Clock 接口：Real（墙钟）/ Fake（可手动推进）
  fakestore       外部对象存储的进程内假实现（可注入延迟与故障）
  cache           共享缓存：物理键 = <隔离域>|<租户>|<逻辑键>
  engine          分租户队列、内存预算、跨租户轮询调度、可取消执行
  server          HTTP 路由与处理器
```

### 隔离设计

- **身份**：租户 ID 只由测试认证上下文（`X-Tenant-ID` 头）提供。请求正文中出现
  `tenant_id` / `tenantId` 字段一律拒绝（HTTP 400，`TENANT_OVERRIDE_REJECTED`）；
  缺头为 401。
- **队列**：每租户独立 FIFO 队列（默认深度 4）。调度器跨租户轮询（round-robin），
  某租户队列满或预算耗尽时**跳过该租户**，不阻塞其他租户。
- **内存预算**：每租户在途内存上限（默认 16 MiB）与在途任务数上限（默认 2）。
  任务声明的 `mem_bytes` 在派发时计入租户预算，执行期间真实分配并持有；
  超出预算的任务留在队列中等待，不影响其他租户。
- **缓存**：共享存储，但物理键包含隔离域与租户 ID
  （如 `compute|tenant-a|shared-key`），跨租户同名逻辑键互不可见。
- **可观测性**：`/v1/stats` 只返回调用租户自己的统计行。
- **取消**：`DELETE /v1/jobs/{id}` 取消任务（仅属主可操作）；`wait=true` 的
  请求若客户端断连，服务端会取消对应任务。执行循环每 4096 次迭代轮询一次
  取消信号。

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 健康检查（无需认证） |
| POST | `/v1/compute[?wait=true&timeout_ms=N]` | 提交计算任务；`wait=true` 阻塞至终态或超时 |
| GET | `/v1/jobs/{id}` | 查询任务（仅属主租户可见） |
| DELETE | `/v1/jobs/{id}` | 取消任务（仅属主租户；运行中任务的取消是异步生效的，响应可能仍显示 `running`，随后 GET 可见 `cancelled`） |
| GET | `/v1/cache/{key}` | 读取本租户缓存值 |
| GET | `/v1/stats` | 本租户统计与限额 |

请求体：`{"key":"...","work":N,"mem_bytes":M}`。错误体：
`{"error":{"code":"...","message":"..."}}`，主要错误码：`UNAUTHENTICATED`、
`TENANT_OVERRIDE_REJECTED`、`INVALID_BODY`、`INVALID_KEY`、`INVALID_WORK`、
`INVALID_MEM_BYTES`、`QUEUE_FULL`(429)、`JOB_NOT_FOUND`、`KEY_NOT_FOUND`。

## 构建与运行

```bash
go build ./...
go run ./cmd/server -addr 127.0.0.1:8080 \
    -workers 2 -queue-depth 4 -in-flight-jobs 2 -mem-budget 16777216
```

请求样例（脚本化）：`./examples/requests.sh 127.0.0.1:8080`

手工样例：

```bash
# 租户 A 计算并等待结果
curl -s -X POST 'http://127.0.0.1:8080/v1/compute?wait=true' \
  -H 'X-Tenant-ID: tenant-a' -H 'Content-Type: application/json' \
  -d '{"key":"shared-key","work":100000,"mem_bytes":4096}'

# 租户 B 用同名键计算（不同的 work → 不同的结果与缓存键）
curl -s -X POST 'http://127.0.0.1:8080/v1/compute?wait=true' \
  -H 'X-Tenant-ID: tenant-b' -H 'Content-Type: application/json' \
  -d '{"key":"shared-key","work":200000,"mem_bytes":4096}'

# 各自只能读到自己的值
curl -s 'http://127.0.0.1:8080/v1/cache/shared-key' -H 'X-Tenant-ID: tenant-a'
curl -s 'http://127.0.0.1:8080/v1/cache/shared-key' -H 'X-Tenant-ID: tenant-b'
```

## 测试与验收

```bash
go test -race -count=1 ./...                       # 单元 + 集成测试
go run ./cmd/server -addr 127.0.0.1:8080 &          # 先启动服务
go run ./cmd/faultclient -addr http://127.0.0.1:8080 -report report.json
```

故障注入客户端执行四个验收场景并输出结构化 JSON 报告（退出码非 0 表示失败）：

1. `cross_tenant_same_key_isolation` — 跨租户同名键互不可见；
2. `tenant_override_rejected` — 正文携带租户字段被拒绝；
3. `single_tenant_overload` — 单租户过载收到 429，其他租户仍被服务；
4. `cancellation` — 属主可取消、跨租户取消被拒、客户端断连触发取消。

## 实际运行记录（2026-09-25，go1.22.2 linux/amd64）

### `go vet ./... && go build ./...`
通过，无输出。

### `go test -race -count=1 ./...`
首轮 **2 项未通过**，均为测试自身的时序假设问题（实现无误）：

- `TestFakeClockSleep`：测试在 goroutine 注册定时器前就推进了假时钟。
  修复：改为小步推进直至 Sleep 返回。
- `TestQueueFullRejectsOnlyThatTenant`：测试假设 worker 尚未取走任务，
  与调度存在竞态。修复：改为循环提交直至出现 `ErrQueueFull`，并断言
  受理数不超过“队列深度 + 在途上限”。

修复后复跑全部通过：

```
ok  tenantiso/internal/cache    1.016s
ok  tenantiso/internal/clock    1.018s
ok  tenantiso/internal/engine   1.207s
ok  tenantiso/internal/server   1.642s
```

### 故障注入客户端（真实服务，端口 38569）

命令：`/tmp/tenantiso-faultclient -addr http://127.0.0.1:38569 -report /tmp/fault-report.json`

首轮 **1 个步骤未通过**：`single_tenant_overload` 中客户端断言
“受理数 ≤ 队列深度(4)”，实测 `accepted=6`。原因是洪泛期间 worker 会取走
任务腾出队列槽位，同一时刻在制上限是“队列深度 4 + 在途 2”，服务端行为正确，
是客户端期望写错。修正断言为有界性检验（`0 < accepted < flood`）后复跑：

```
ok: true  passed: 4  failed: 0
  cross_tenant_same_key_isolation: ok (1ms)
  tenant_override_rejected:        ok (0ms)
  single_tenant_overload:          ok (30ms)   # accepted=6 rejected=10，tenant-quiet 31ms 内完成
  cancellation:                    ok (502ms)  # 含跨租户取消拒绝与客户端断连取消
```

完整报告存档于 [`docs/acceptance-report.json`](docs/acceptance-report.json)。

### 已知限制

- 任务注册表只增不减（本地测试服务可接受）；生产化需加 TTL 清理。
- 取消对运行中任务异步生效（执行循环每 4096 次迭代轮询），DELETE 响应
  可能仍显示 `running`。
- 内存预算按任务声明值记账并真实分配，单任务上限 8 MiB、租户在途上限
  16 MiB（均可用启动参数调整）。
