# 优雅停机阶段控制 (Graceful Shutdown Phase Control)

纯后端演示项目（Go，标准库，零第三方依赖）：本地 HTTP 服务 + 故障注入客户端。
所有“外部依赖”均为**进程内假服务**（Fake DB / Fake Queue），不连接任何生产系统。
提供可控时钟（真实时钟 / 可手动推进的假时钟）与结构化测试结果。

## 四阶段停机模型

```
接收 receiving ──停机信号(同步生效)──▶ 排空 draining
                                          │
                      在排空超时内完成 ◀───┤
                                          ▼
                                   超时取消 cancelling  (cancel 所有在途请求 ctx)
                                          │
                                          ▼
                                    关闭资源 closing    (LIFO: fake-queue → fake-db)
                                          │
                                          ▼
                                       done
```

1. **接收 (receiving)**：正常处理请求。停机信号到来时**同步**进入排空阶段——信号返回后保证不再接收任何新工作（含后台任务）。
2. **排空 (draining)**：等待所有已接收的在途工作完成，受 `--drain-timeout` 上界约束。
3. **超时取消 (cancelling)**：排空超时后，cancel 仍在途工作的 context；每个已接收请求必须有 `completed` 或 `cancelled` 的**明确终态**。
4. **关闭资源 (closing)**：按 LIFO（注册逆序）关闭资源并记录顺序。

停机信号（`POST /shutdown` 或 SIGINT/SIGTERM，可重复）完全幂等：多次触发只执行一次，次数计入报告。

## 启动就绪与存活端点分离

- `GET /healthz`（存活）：进程在就返回 200，**停机期间也保持 200**。
- `GET /readyz`（就绪）：接收阶段 200；停机信号后立即 503（`draining`/…/`done`）。

负载均衡器/编排系统据此在排空开始前摘流量，而存活探针不会在优雅停机期间误杀进程。

## 目录结构

```
cmd/server/         HTTP 服务入口（信号处理、四阶段编排、退出）
cmd/faultclient/    故障注入客户端：长请求/流请求/重复信号交错，输出结构化 JSON 报告
internal/clock/     可控时钟：Real（真实）+ Fake（手动推进、确定性定时器）
internal/shutdown/  停机协调器：接收闸门、排空、超时取消、幂等信号
internal/server/    HTTP 处理器：/healthz /readyz /work /stream /task /shutdown /state
internal/fakesvc/   进程内假外部服务：FakeDB（延迟查询）、FakeQueue（后台任务）
internal/resources/ 资源注册表：LIFO 关闭并记录顺序
internal/ledger/    请求台账：每个已接收工作的结构化终态记录
examples/           请求样例与真实运行报告
```

## 快速开始

需要 Go 1.22+。

```bash
# 1. 启动服务
go run ./cmd/server -addr 127.0.0.1:8080 -drain-timeout 2s

# 2. 另开终端，运行故障注入场景（自动驱动整轮验收并输出 JSON）
go run ./cmd/faultclient -base http://127.0.0.1:8080 -out report.json
# 退出码 0 = 全部断言通过；非 0 = 有检查失败
```

命令行参数：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `-addr` | `127.0.0.1:8080` | 监听地址 |
| `-drain-timeout` | `2s` | 排空阶段最长等待时间，超时进入取消阶段 |
| `-post-done-grace` | `3s` | 停机完成后 `/state`、`/healthz` 继续可访问的宽限期，随后进程退出 |

请求样例见 [`examples/requests.md`](examples/requests.md)。

## 验收场景（故障注入客户端）

客户端在服务仍接收时发起：一个长请求（800ms）、一个流请求（4×150ms）、一个超长请求（30s）、
一个后台任务；随后交错发送**两次**重复停机信号，并断言：

- 停机前 `/healthz`、`/readyz` 均为 200；停机后就绪 503、存活仍 200；
- 停止接收后新请求 503、新后台任务 503（**停止接收后不新增后台任务**）；
- 排空窗口内的长请求、流请求 **200 completed**；
- 超长请求收到 **499 + `{"outcome":"cancelled","reason":"context canceled"}`**（明确取消）；
- 阶段序列严格为 `receiving → draining → cancelling → closing → done`；
- 资源按 LIFO 关闭：`fake-queue(1) → fake-db(2)`；
- 台账中每个已接收工作都有终态，且 completed / cancelled 两种结果都实际观察到；
- 重复停机信号被计数但只执行一次。

真实运行结果见 [`examples/sample-report.json`](examples/sample-report.json)（21 项检查全部通过）。

## 自动化测试

```bash
go test ./...              # 单元 + HTTP 集成测试（fake clock 确定性驱动超时）
go test -race ./...        # 竞态检测
go test -cover ./...       # 覆盖率
```

测试内容：

- `internal/clock`：假时钟定时器到期边界、Stop、Sleep（确定性时间推进）；
- `internal/shutdown`：窗口内排空成功、超时取消、停止接收后拒绝请求与后台任务、
  重复信号幂等、阶段顺序、LIFO 关闭；
- `internal/server`：存活/就绪分离、长请求+流请求+重复信号完整交错场景、
  `/state` JSON 结构。

## 实际运行记录

在本机（Go 1.22.2, linux/amd64）实际执行的命令与结果：

```bash
$ go vet ./... && go build ./...        # 无输出（通过）
$ go test ./...                          # 全部 ok
$ go test -race -count=2 ./internal/...  # 全部 ok（无 data race）
$ go run ./cmd/faultclient -base http://127.0.0.1:8080   # 21/21 checks passed, exit 0
$ kill -TERM <pid> (×2)                  # 信号路径：exit 0，长请求完成、超长请求明确取消
```

开发过程中如实发现并修复的问题：

1. 假时钟在定时器 goroutine 注册前被 `Advance` 导致测试竞态——增加 `Pending()` 同步；
2. 初版“停止接收”在异步 goroutine 中切换阶段，存在信号返回后仍可接收新工作的窗口——
   改为在 `Shutdown()` 返回前**同步**切换到 draining 阶段；
3. OS 信号 goroutine 只读一次信号，重复 SIGTERM 不计入——改为持续消费、幂等触发。

当前无未通过项。

## 设计说明

- **可控时钟**：生产用 `clock.Real`；测试用 `clock.Fake`（手动 `Advance`），
  使“排空超时 → 取消”无需真实等待，确定性可复现。
- **终态台账 (ledger)**：每个被 `TryAccept` 接纳的工作单元都有 ID、开始/结束时间与
  `completed|cancelled` 终态，杜绝“静默消失”。
- **取消传播**：协调器持有根 context，取消阶段 cancel；HTTP 处理器将其与客户端
  断连 context 合并，取消原因如实写入响应与台账。
- **纯后端**：无任何前端代码/页面。
