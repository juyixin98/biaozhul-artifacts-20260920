# 运行结果记录

> 记录时间：2026-09-24；环境 `go1.23.4 linux/amd64`（Ubuntu, kernel 6.8）。以下结果均为本机实际执行所得，原始日志见 `testlogs/`。

## 复现命令

```bash
go vet ./...                  # 静态检查
gofmt -l .                    # 输出为空
go build ./...                # 构建
go test -race ./...           # 全部测试 + 竞态检测
go test -cover ./...          # 覆盖率
examples/demo.sh              # 端到端验收演示（自动选空闲端口）
```

## 实际结果

| 检查 | 结果 |
|---|---|
| `go vet ./...` | 干净，无告警 |
| `gofmt -l .` | 空（全部已格式化） |
| `go build ./...` | 成功 |
| `go test -race ./...` | **全部通过**，无数据竞争（`gang` 包 15 个、`api` 包 7 个，共 22 个测试） |
| `go test -cover ./...` | `gang` **93.9%**，`api` **93.2%**（`main` 包为装配代码未测） |
| 并发压力 | `go test -race -count=20 -run TestHTTPConcurrentNoOverbook`（20 组×2 槽抢 4 节点，重复 20 轮）**全部通过，无超卖** |
| `examples/demo.sh` | **29 passed, 0 failed**，退出码 0 |

## 验收场景实际验证到的保证

对应题目要求“两个组竞争交叠节点，在预留与提交间下线节点”：

1. 节点 n1(role=a)、n2(role=shared)、n3(role=b)，各 1 槽。
2. gA 需要 n1+n2：原子预留成功（plan p1，带 version 令牌）。
3. gB 需要 n2+n3：**整体 waiting，`plan:null`，连空闲的 n3 也一个槽不占**。
4. 在 gA 的预留与提交之间把 n2 下线：接口返回 200，gA 的计划被**整体** abort；随后 commit 返回 **410 Gone**。
5. 立即查 `/state`：三个节点 `held_slots` 与 `running_slots` **全为 0**——无部分启动、无资源泄漏。
6. n2 恢复上线：gA 自动重新预留（新 plan + 新 version）→ commit 200 运行；对运行中的 n2 下线被 **409** 拒绝；complete 后 gB **原子拿到恰好 n2+n3** 并成功运行——无重复占用（n2 上只有 gB 一个运行槽）。
7. 另验证：陈旧 version 提交 → **409**，计划整体中止、零槽启动；对已中止计划再次提交 → **410**；TTL 2 秒的预留被后台扫描器回收为 `expired`，之后提交 → 410。

HTTP 层同样有一个完整端到端测试 `TestHTTPAcceptanceFlow` 复现上述场景（`api/server_test.go`）。

## 过程中发现并修复的真实缺陷（测试驱动）

1. **预留版本快照时机错误**：节点 version 在“持有槽位”之后才 bump，但计划的乐观版本快照取自 bump 之前，导致正常 commit 恒冲突。改为持有后快照。
2. **同组多任务可重复选同一节点**：贪心选节点时未计入“本次选择已暂占”的槽，capacity=1 的节点可能被同组 2 个任务重复占用（直接违反一任务一槽）。加入 tentative 占用计数修复。
3. **计划失效后归属判断错误**：commit 查计划要求 gang.ActivePlanID 仍指向它，但失效路径已清空该字段，导致对失效计划的提交返回 409 而非语义正确的 410。改为只按 plan 自身的 GangID 判定归属。
4. **release 语义**：原实现 release 后本组又被自动重新预留，与“释放给其它等待者”的直觉相悖且会饿死后面的组。改为 release 后本组退出调度队列，需显式 `POST /retry`。

## 演示脚本的一次踩坑（已修复，保留说明）

首次运行 `examples/demo.sh` 失败：脚本默认端口 18080 在本机被一个无关服务 `hlc-server` 长期占用，新服务 `bind: address already in use` 但脚本未检测启动失败，请求打到了别人的服务上。修复为：自动选取空闲回环端口、启动后轮询确认是本服务（校验 `/state` 含调度器字段）、进程提前退出立即报错。

## 未完成 / 已知限制

- 进程内内存状态，无持久化、无多实例/分布式锁。
- 单 mutex、单实例：实现强原子性与可验证性，牺牲了大规模吞吐。
- 调度为确定性贪心，无装箱优化、抢占、配额。
- `expired` 为终态，需客户端重新提交；`release` 后需显式 retry（有意为之）。
- 无鉴权/TLS；资源模型为同构槽位，非多维资源。
