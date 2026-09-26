# tenantiso — 多租户执行隔离（纯后端演示）

本项目演示 CPU 计算请求的多租户执行隔离：每个租户拥有独立 FIFO 队列与内存预算，
共享 worker 池按租户轮询调度；共享缓存的物理键内嵌隔离域与租户 ID，跨租户同名键互不污染。
所有外部依赖均为**本进程内假服务**，不接触任何生产系统。

## 设计要点

| 关注点 | 实现 |
|---|---|
| 租户身份 | 仅由测试认证上下文（`X-Tenant-ID` 头，代替真实身份令牌）提供；请求正文中出现 `tenant_id`/`tenantId`/`tenant` 字段一律 400 拒绝 |
| 分租户队列 | 每租户 FIFO 队列，`MaxQueuePerTenant` 限制在途任务数，超限 429；调度器跨租户轮询，单租户过载不波及其他租户 |
| 内存预算 | 每租户在途任务 `mem_bytes` 总和不得超过 `MemBudgetPerTenant`，超限 429；任务终态时释放预留 |
| 取消 | `DELETE /v1/jobs/{id}`，仅属主租户可取消；排队任务同步置为 canceled，运行中任务通过 context 取消（异步收敛到 canceled） |
| 共享缓存隔离 | 物理键 = `tenantiso-v1`（隔离域）+ 长度前缀租户 ID + 用户键；长度前缀防止边界伪造碰撞 |
| 跨租户访问 | 读取/取消他租户任务返回 404（不泄露任务 ID 存在性） |
| 可控时钟 | `clock.Clock` 接口；测试用 `Fake` 手动推进，任务时间戳全部取自时钟接口 |
| 故障注入 | 假后端支持注入延迟与强制失败，由 `faultclient` 经 `PUT /admin/faults` 控制 |
| 结构化测试结果 | `scripts/test.sh` 产出 `results/test-results.json`（`go test -json` 原始流）与 `results/summary.json`（分包汇总） |

## 目录结构

```
cmd/server/        本地 HTTP 服务入口
cmd/faultclient/   故障注入客户端（set/show 注入延迟与失败次数）
internal/auth/     测试认证上下文（租户身份唯一来源）
internal/cache/    共享缓存（键含隔离域 + 租户 ID）
internal/backend/  外部计算服务的进程内假实现（含故障注入）
internal/jobs/     分租户队列、内存预算、轮询调度、取消
internal/server/   HTTP 路由与处理器
internal/clock/    可控时钟（Real / Fake）
internal/testsum/  go test -json 结果汇总器
examples/requests.sh  请求样例（curl）
scripts/test.sh       结构化测试运行器
```

## 运行

```bash
go build ./...
go run ./cmd/server -addr 127.0.0.1:8080 \
  -workers 2 -max-queue-per-tenant 4 -mem-budget-per-tenant 1048576
```

另开一个终端执行请求样例：

```bash
./examples/requests.sh http://127.0.0.1:8080
```

## API 与请求样例

### 提交计算任务（需租户头）

```bash
curl -X POST http://127.0.0.1:8080/v1/compute \
  -H 'X-Tenant-ID: tenant-a' -H 'Content-Type: application/json' \
  -d '{"key":"report","payload":"quarterly-numbers","iterations":1000,"mem_bytes":4096}'
# 202 Accepted
# {"id":"job-1","tenant_id":"tenant-a","key":"report","iterations":1000,
#  "mem_bytes":4096,"status":"queued","created_at":"...",...}
```

### 正文覆盖租户 → 400

```bash
curl -X POST http://127.0.0.1:8080/v1/compute \
  -H 'X-Tenant-ID: tenant-a' \
  -d '{"tenant_id":"tenant-b","key":"x","payload":"p","iterations":1,"mem_bytes":1}'
# 400 {"error":"tenant identity is set by the auth context and cannot be overridden by the request body"}
```

### 缺少租户头 → 401

```bash
curl -X POST http://127.0.0.1:8080/v1/compute \
  -d '{"key":"x","payload":"p","iterations":1,"mem_bytes":1}'
# 401 {"error":"missing tenant identity"}
```

### 查询 / 取消任务

```bash
curl -H 'X-Tenant-ID: tenant-a' http://127.0.0.1:8080/v1/jobs/job-1
curl -X DELETE -H 'X-Tenant-ID: tenant-a' http://127.0.0.1:8080/v1/jobs/job-1
# 他租户访问同一 id → 404 {"error":"job not found"}
```

### 读取租户缓存

```bash
curl -H 'X-Tenant-ID: tenant-a' http://127.0.0.1:8080/v1/cache/report
# 200 application/octet-stream（本租户同名键的值；他租户读到的是各自条目或 404）
```

### 故障注入（faultclient）

```bash
go run ./cmd/faultclient -addr http://127.0.0.1:8080 -fail-next 1   # 下一次计算强制失败
go run ./cmd/faultclient -addr http://127.0.0.1:8080 -latency 5s    # 每次计算注入 5s 延迟
go run ./cmd/faultclient -addr http://127.0.0.1:8080 -latency 0s    # 清除
go run ./cmd/faultclient -addr http://127.0.0.1:8080 -show          # 查看当前故障设置
```

## 测试

```bash
./scripts/test.sh          # 结构化结果：results/test-results.json + results/summary.json
go test -race ./...        # 竞态检测
go test -cover ./...       # 覆盖率
```

验收场景对应自动化测试（`internal/server/server_test.go`）：

- `TestCrossTenantSameKeyIsolation` — 跨租户同名键各自读到自己的值
- `TestTenantOverrideRejected` / `TestMissingTenantRejected` — 正文覆盖 400、缺租户头 401
- `TestSingleTenantOverloadOthersServed` — 单租户过载 429，其他租户仍被受理并完成
- `TestMemoryBudgetEnforced` — 内存预算超限 429
- `TestCancellation` — 取消排队任务并释放队列槽位与内存预留
- `TestCrossTenantJobAccessDenied` — 跨租户读/取消 404
- `TestFaultInjection` — 注入失败 → 任务 failed，清除后恢复
- `TestCacheMissIsPerTenant` — 缓存未命中按租户隔离

## 实际运行记录（如实）

环境：go1.22.2 linux/amd64。

### 单元/集成测试

```
$ ./scripts/test.sh
PASS example.com/tenantiso/internal/auth (passed=2 failed=0 skipped=0)
PASS example.com/tenantiso/internal/cache (passed=3 failed=0 skipped=0)
PASS example.com/tenantiso/internal/clock (passed=2 failed=0 skipped=0)
PASS example.com/tenantiso/internal/jobs (passed=4 failed=0 skipped=0)
PASS example.com/tenantiso/internal/server (passed=10 failed=0 skipped=0)
structured summary written to results/summary.json
```

```
$ go test -race -count=3 ./...
ok  example.com/tenantiso/internal/auth     1.019s
ok  example.com/tenantiso/internal/cache    1.021s
ok  example.com/tenantiso/internal/clock    1.026s
ok  example.com/tenantiso/internal/jobs     1.018s
ok  example.com/tenantiso/internal/server   3.817s
```

```
$ go test -cover ./...
ok  internal/auth    coverage: 100.0% of statements
ok  internal/cache   coverage: 85.2% of statements
ok  internal/clock   coverage: 100.0% of statements
ok  internal/jobs    coverage: 54.0% of statements
ok  internal/server  coverage: 78.8% of statements
```

### 开发中发现并已修复的问题

1. **数据竞态（`-race` 检出）**：`Submit`/`Get` 直接返回共享 `*Job`，HTTP 层 JSON 编码时
   worker 正在写入同一结构体。修复为在锁内返回快照副本。修复后 `-race -count=3` 全部通过。
2. **运行中任务的取消为异步**：`DELETE` 对 running 任务先返回当前快照（status 仍为
   running），worker 观察到 context 取消后置为 canceled。`examples/requests.sh` 第 8 步
   通过轮询展示终态 `canceled`。

### 请求样例实测输出（节选，端口 18231）

```
== 1. tenant-a 提交 ==  {"id":"job-1","tenant_id":"tenant-a","key":"report","status":"queued",...}
== 2. tenant-b 同名键 == {"id":"job-2","tenant_id":"tenant-b","key":"report","status":"queued",...}
== 3. 正文覆盖租户 ==   HTTP 400 {"error":"tenant identity is set by the auth context ..."}
== 4. 缺租户头 ==       HTTP 401 {"error":"missing tenant identity"}
== 5. 同名键各自缓存 ==  tenant-a: fba9cc30103e4a7a   tenant-b: dbb76c436b0c4044   （互不相同）
== 6. 跨租户访问 ==     HTTP 404 {"error":"job not found"}
== 7. 故障注入 ==       fail_next=1 → job-3 "status":"failed","error":"injected backend failure"
== 8. 取消 ==           DELETE job-5（running）→ 轮询终态 final status of job-5: canceled
== 9. 任务状态 ==       job-1 "status":"succeeded"
```

### 未通过项 / 已知限制

- 当前无未通过的测试。
- `internal/jobs` 覆盖率 54.0%，低于 80% 目标：调度循环的关闭路径与运行中取消分支
  缺少确定性单测（并发时序难以稳定复现），已由服务端集成测试间接覆盖。
- 演示期间本机 18080/18099 端口被其他进程（cbdemo、java）占用，实测改用了 18231；
  这不是项目缺陷，但 `examples/requests.sh` 的默认端口如被占用需自行更换。
- 取消运行中任务不是即时的（见上“已知问题 2”）。
- 假后端的计算结果为 8 字节混合摘要，仅用于演示，无密码学意义。
