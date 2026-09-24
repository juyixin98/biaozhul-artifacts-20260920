# Gang Scheduler —— 成组任务原子调度器（Go / net/http）

纯后端服务。一组任务（gang）必须 **一次性拿到全部所需节点**，否则一个都不占、继续等待。
预留采用两阶段提交并带**乐观版本检查**：预留成功后返回版本号，提交（commit）时若任一被选中节点在这期间发生变化（下线等），**整组中止，绝不部分启动**。

特性：

- **成组原子调度（all-or-nothing）**：放不下时整组 `waiting`，不持有任何节点；
- **两阶段预留**：`POST /gangs` 预留 → `commit` 转运行；预留带 **TTL**，后台扫描器自动回收；
- **节点标签选择**（`node_selector`，键值全等匹配）与**反亲和**（`anti_affinity`：同组任务所落节点的标签值互不相同，内置键 `zone` 回退节点 zone 字段）；
- **乐观并发版本检查**：每个节点带单调 `version`，计划带全局 `version` 令牌；commit 在同一把锁内重检，冲突即整体失败；
- **节点上下线**：运行中的节点拒绝下线（409）；仅被预留下线时，相关计划**整体失效**（410），占用全部释放，等待组立即重新调度；
- **无资源泄漏/无重复占用**：单 mutex 线性化所有变更，配套不变量测试（`assertNoLeaks`）+ `go test -race` 并发压测；
- 仅依赖 **Go 标准库**（`net/http`，Go 1.22+ 的方法路由），无任何第三方包。

## 目录结构

```
.
├── go.mod              # 模块声明，无第三方依赖（因此无 go.sum）
├── main.go             # 入口：HTTP 服务、优雅退出、访问日志
├── gang/
│   ├── types.go        # 领域模型与错误哨兵
│   ├── scheduler.go    # 调度核心：原子预留、两阶段提交、TTL、队列
│   ├── views.go        # 只读 JSON 快照
│   └── scheduler_test.go
├── api/
│   ├── server.go       # net/http 路由与 JSON 编解码
│   └── server_test.go  # 端到端 HTTP 测试（含验收场景与并发测试）
└── examples/
    ├── demo.sh         # 一键跑通验收场景
    └── requests.http   # 全部接口的 curl 样例
```

## 依赖与启动

依赖：Go 1.22+（开发与验证环境为 go1.23.4 linux/amd64），构建/测试不需要网络。

```bash
# 构建
go build ./...

# 启动（默认 :8080；预留默认 TTL 15s；后台回收扫描间隔 200ms）
go run .
# 或指定参数
go run . -addr=:9090 -ttl=30s -sweep=500ms
```

启动后：

```bash
curl -s localhost:8080/healthz      # {"status":"ok"}
curl -s localhost:8080/state | jq . # 集群快照
```

锁定依赖：`go.mod` 声明 `go 1.23`，`go.mod` 中没有任何 `require`，标准库之外零依赖，
因此没有也不需要 `go.sum`（无任何可下载模块；`go mod verify` 对纯标准库模块无输入）。

## HTTP 接口

| 方法 & 路径 | 说明 |
|---|---|
| `GET /healthz` | 健康检查 |
| `GET /state` | 集群一致性快照（节点/组/计划/等待队列/generation） |
| `POST /nodes` | 注册节点 |
| `GET /nodes/{id}` | 查询节点 |
| `POST /nodes/{id}/state` | 上线/下线 `{"online":bool}` |
| `POST /gangs` | 提交组，立即尝试原子预留 |
| `GET /gangs/{id}` | 查询组与当前计划 |
| `POST /gangs/{id}/retry` | waiting 组显式重试预留 |
| `POST /gangs/{id}/plans/{plan}/commit` | 提交计划，body 必须含预留返回的 `version` |
| `POST /gangs/{id}/plans/{plan}/release` | 放弃预留 |
| `POST /gangs/{id}/complete` | 运行完成，释放节点 |

状态码：`200/201` 成功；`400` 请求非法；`404` 不存在；`405` 方法不允许；
`409` 状态或乐观版本冲突（含对运行中节点下线）；`410` 预留已过期或被拓扑变更失效。
完整 curl 样例见 [`examples/requests.http`](examples/requests.http)。

### 数据模型要点

节点：`id`、`zone`、`labels`、`capacity`（任务槽位数，默认 1）。

组：`id`、`min_nodes`、`tasks`（要求 `len(tasks)==min_nodes`，一个任务占一槽）、
每任务可选 `node_selector`、组级 `anti_affinity`、可选 `ttl_millis`。

组状态：`waiting → reserved → running → succeeded`；预留超时/被下线波及 → `expired`
（终态，需重新提交；主动 `release` 则回到 `waiting`）。

### 原子性与一致性是怎么保证的

1. **单把互斥锁**线性化所有状态变更；预留选择节点、持有槽位、生成计划在同一临界区内完成。
2. **选择阶段只读**：贪心（任务按提交顺序、候选节点按 id 排序）逐任务选节点，
   选槽位时计入本次选择中本计划暂占的槽，任何一个任务放不下则整组放弃、不写任何状态。
3. **commit 重检**：提交时在锁内校验 (a) 计划仍为 reserved、(b) TTL 未过、
   (c) 计划版本令牌一致、(d) 每个被选节点仍在线且 `version` 与预留快照一致。
   任一不满足 → 释放该计划**全部**槽位并整体失败，节点状态转换只在全部校验通过后一次性发生。
4. **节点下线**：有 running 槽直接 409；仅 held 时，把涉及的每个计划完整失效
   （释放其在所有节点上的槽），因此不存在“下线一个节点、计划半死不活”。
5. **TTL 回收**：后台扫描器（默认每 200ms）原子地将到期计划标记过期并释放全部槽位；
   随后的 commit 得到 410。

## 自动化测试

```bash
go test -race ./...       # 全部单测 + 端到端 HTTP 测试，开启竞态检测
go test -cover ./...      # 覆盖率
```

测试覆盖（22 个测试）：

- 原子预留/提交 happy path；放不下整组等待且零持有；
- **验收场景**：两个组竞争交叠节点（n1+n2 vs n2+n3），第二组等待不占任何节点，第一组完成后原子拿到 n2+n3，无重复占用；
- **预留与提交间下线节点**：commit 410、零部分启动、零泄漏；节点恢复后重新预留提交成功；
- 乐观版本不符 → 409 且整计划中止；节点级 version 变化（保持在线）→ 409；对中止计划重复提交 → 410；
- TTL 到期：假时钟单测 + 真实后台扫描器 HTTP 测试（等待过期、槽位释放）；
- 标签选择与 zone 反亲和、不可满足的反亲和整组等待；运行中节点拒绝下线；
- release/retry、重复 commit、参数校验、404/400/405 错误映射；
- 20 个组并发抢 4 个单槽节点的 HTTP 压测（`-race`），断言无节点超卖；
- 每个关键步骤后运行 `assertNoLeaks` 全局资源不变量：槽位计数双向一致、held 必属于 reserved 计划、running 必属于 running 组。

## 一键验收演示

```bash
examples/demo.sh
```

脚本启动临时服务（TTL 2 秒），依次演示：竞争交叠节点、预留与提交间下线节点（410 + 零泄漏）、
恢复后 gA 完成、gB 原子拿到 n2+n3、反亲和、陈旧版本 409、TTL 过期 410，并在最后打印
`X passed / Y failed`。需要 `curl` 与 `python3`（仅用于格式化/提取字段）。

## 已知限制 / 未完成项

- **进程内内存状态**：重启即丢；未做持久化与多实例（要水平扩展需要把锁/状态换成 etcd 等）。
- 单调度器实例、单把互斥锁；节点规模/并发极大时是吞吐瓶颈（换取的是实现可验证的强原子性）。
- 调度策略为确定性贪心（任务顺序 + 节点 id），不做容量装箱优化、抢占（preemption）与公平性配额。
- `expired` 为终态：超时后需要客户端重新 `POST /gangs`，不会自动重新排队（主动 release 才回到 waiting）。
- 无鉴权/TLS，定位为本地/可信网络内的调度内核与演示服务。
- 节点容量为同构槽位模型；未实现 GPU/多维资源（CPU/内存）等多维背包。
