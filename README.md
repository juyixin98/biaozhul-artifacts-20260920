# conditionupdate — 条件更新竞争（ETag / If-Match）纯后端演示

用 Go 实现的本地 HTTP 服务，演示基于 ETag 的乐观并发控制（条件更新）。
所有外部依赖均为**本进程内假服务**，不接任何生产系统；时钟可控；
故障注入客户端以结构化 JSON 输出验收结果。

## 功能与语义

- **强 ETag**：格式 `"<id>-v<版本>-<内容哈希前8位>"`，版本与内容在单把互斥锁内**原子**变更，
  读者永远不会观察到“新版本号 + 旧内容”的撕裂状态。
- **If-Match**：更新/删除必须携带。强比较——弱 ETag（`W/"..."`）即使 opaque 值相同也**不匹配**（412）。
- **If-Match: \***：匹配任意已存在资源；资源不存在时 412。
- **If-None-Match: \***：仅当资源不存在时创建（create-only）。
- **缺少前置条件**：PUT/DELETE 不带任何条件头 → **428 Precondition Required**。
- **删除重建**：删除后版本重置，旧 ETag 永不复用，携带旧 ETag 的写 → 412。
- **失败无副作用**：下游（假审计服务）在被注入故障时，更新整体中止——
  存储与审计日志均无任何变化（钩子运行在存储原子区内，失败即放弃提交）。

## 目录结构

```
cmd/server/        HTTP 服务入口
cmd/faultclient/   故障注入客户端（验收场景运行器，输出结构化 JSON）
internal/clock/    可控时钟（Real / Fake）
internal/etag/     ETag 解析与强比较（弱标签永不满�足强比较）
internal/store/    内存资源存储，原子 compare-and-swap
internal/fakesvc/  本进程假下游服务（审计日志）+ 故障注入
internal/httpserver/ HTTP 路由与管理端点
samples/requests.sh  请求样例脚本
```

## API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/resources/{id}` | 读取，响应带 `ETag` 头；不存在 404 |
| PUT | `/resources/{id}` | 条件创建/更新；需 `If-Match` 或 `If-None-Match`，否则 428；不匹配 412 |
| DELETE | `/resources/{id}` | 条件删除；需 `If-Match`，否则 428；成功 204 |
| GET | `/healthz` | 健康检查 |
| GET | `/admin/state` | 导出全部资源（测试观测用） |
| GET | `/admin/audit` | 假审计服务已记录的条目 |
| GET/POST | `/admin/faults` | 查看/设置故障注入 `{"failNext":N,"latencyMs":M}` |
| POST | `/admin/clock` | 可控时钟 `{"set":"RFC3339","advanceMs":N}`（仅 `-fake-clock` 时） |

错误统一为 `{"error":{"code":"...","message":"..."}}`。

## 运行

```bash
go build ./...
go test -race -count=1 -cover ./...

# 终端 1：启动服务（默认 127.0.0.1:8080，可控时钟开启）
go run ./cmd/server -addr 127.0.0.1:18080

# 终端 2：故障注入客户端，跑全部验收场景，输出结构化 JSON
go run ./cmd/faultclient -server http://127.0.0.1:18080

# 或逐条手动请求
bash samples/requests.sh http://127.0.0.1:18080
```

## 验收场景（faultclient 输出字段）

| 场景 | 期望 |
|------|------|
| `concurrent-same-version-one-wins` | 两个并发同版本更新：恰好一个 200、一个 412 |
| `delete-recreate-stale-etag-rejected` | 删除重建后旧 ETag 写入被拒（412） |
| `wildcard-if-match` | `If-Match: *` 已存在 200 / 不存在 412 |
| `missing-precondition-428` | 无前置条件 → 428 |
| `weak-etag-no-strong-match` | 弱 ETag 强比较 → 412 |
| `fault-injection-no-side-effects` | 注入下游故障 → 503 且 ETag/审计无变化 |

## 实际运行记录（2026-09-25，本机 go1.22.2 linux/amd64）

```text
$ go vet ./... && go build ./...        # 通过，无输出
$ go test -race -count=1 -cover ./...
ok  conditionupdate/internal/clock       coverage: 100.0%
ok  conditionupdate/internal/etag        coverage: 83.7%
ok  conditionupdate/internal/fakesvc     coverage: 100.0%
ok  conditionupdate/internal/httpserver  coverage: 86.7%
ok  conditionupdate/internal/store       coverage: 88.7%
（cmd/* 为主程序，无单测；竞争检测器全程无报告）

$ ./bin/server -addr 127.0.0.1:18080 &
listening on http://127.0.0.1:18080 (fake-clock=true)

$ ./bin/faultclient -server http://127.0.0.1:18080   # EXIT=0
passed: true —— 6/6 场景通过：
  concurrent-same-version-one-wins      statuses=[200 412]
  delete-recreate-stale-etag-rejected   staleWriteStatus=412
  wildcard-if-match                     existing=200, missing=412
  missing-precondition-428              status=428
  weak-etag-no-strong-match             status=412
  fault-injection-no-side-effects       status=503, ETag unchanged=true

$ curl -s -X POST .../admin/clock -d '{"set":"2026-09-25T10:00:00Z","advanceMs":5000}'
{"now":"2026-09-25T10:00:05Z"}
```

**未通过项：最终无。** 过程中发现并修复两个问题，均已复验：

1. 首次端到端联调时因 18080 端口被残留进程占用，faultclient 全部收到 404——
   环境问题，清理占用后复跑通过。
2. 对同一服务**第二次**运行 faultclient 时场景全部失败（create 收到 412）——
   原因是场景使用固定资源 ID 却没有清理上一次的残留。已在每个场景前加
   `If-Match: *` 清理步骤，场景现在幂等可重跑，连续运行两次均 6/6 通过。
3. 自查 RFC 7232 §3.1 时补正两处边角语义：删除不存在资源时**先评估前置条件**
   （携带无法匹配的 If-Match 返回 412 而非 404）；If-None-Match 改用**弱比较**
   函数（弱标签同值也能阻止写入）。修改时一度引入 nil 指针解引用，已由新增
   单元测试捕获并修复（`-race` 全量复测通过）。

完整结构化输出样例见 `samples/faultclient-output.json`。
