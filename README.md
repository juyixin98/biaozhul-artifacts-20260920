# vcreg — 向量时钟多版本寄存器（本地多副本模拟）

一个纯后端服务：在**单个进程内模拟多个相互隔离的副本**，用
[向量时钟（Vector Clock）](https://en.wikipedia.org/wiki/Vector_clock)对写操作做偏序判定：

- **因果覆盖**：时钟 `a < b`（a happened-before b）时，新版本替换旧版本；
- **并发写**：时钟不可比较时，两个版本作为**兄弟版本（siblings）同时保留**，绝不静默丢数据；
- **显式合并**：客户端带上"合并上下文"（要合并哪些兄弟版本 id）产生一个因果上覆盖它们的新版本；
- **消息语义**：基于版本 id 的去重让投递天然**幂等**，迟到的旧写被标记为 `superseded` 且不会复活。

只使用 Go **标准库**（`net/http`、`encoding/json`），无任何第三方依赖。无界面。

---

## 1. 依赖与启动

| 项 | 要求 |
| --- | --- |
| Go | ≥ 1.23（路由用了 `net/http` 的方法模式 `GET /path/{id}`） |
| 运行时依赖 | **无**（仅标准库） |
| 运行示例脚本 | `curl`、`jq` |

```bash
# 启动（默认 127.0.0.1:18080）
go run .

# 或指定地址
go run . -addr 127.0.0.1:19000
```

依赖锁定说明：`go.mod` 中没有任何 `require` 条目，因此**不存在 `go.sum`**
——零外部依赖即最严格的"锁定"（标准库版本随 Go 工具链固定）。
可以用下面命令复核，输出只包含本模块自身：

```bash
go list -deps -f '{{if not .Standard}}{{.ImportPath}}{{end}}' .
# vcreg
```

## 2. 测试

```bash
go test -v ./...        # 全部测试
go test -race ./...     # 竞态检测（含并发压力测试）
go test -cover ./...    # 覆盖率（当前约 94%）
```

## 3. 一键演示

另开一个终端（服务需已启动）：

```bash
./examples/demo.sh
# 端口不同则：BASE=http://127.0.0.1:19000 ./examples/demo.sh
```

脚本按顺序跑验收三场景 + 一个补充场景，每步都有注释和期望结果。
真实运行记录见 [`RUNLOG.md`](./RUNLOG.md)。

---

## 4. HTTP 接口

所有请求/响应均为 JSON（`Content-Type: application/json`）。版本对象长这样：

```json
{
  "id": "A-2",
  "origin": "A",
  "clock": {"A": 2, "B": 1},
  "value": "resolved=A+B"
}
```

`id` 形如 `<副本>-<该副本事件序号>`，全局唯一；`clock` 即该写的向量时钟。

### 4.1 副本管理

| 方法 & 路径 | 说明 | 请求体 |
| --- | --- | --- |
| `GET  /health` | 健康检查 | — |
| `GET  /replicas` | 列出所有副本 | — |
| `POST /replicas` | 创建副本 | `{"id":"A"}` |
| `DELETE /replicas/{id}` | 删除副本 | — |
| `GET  /replicas/{id}` | 副本全量快照（时钟 + 所有 key 的版本集合） | — |
| `POST /replicas/reset` | 清空所有副本（测试/演示用） | — |

### 4.2 读写

| 方法 & 路径 | 说明 |
| --- | --- |
| `PUT /replicas/{id}/keys/{key}` | **本地写**：`{"value":"..."}`。新版本因果覆盖当前全部兄弟版本。 |
| `GET /replicas/{id}/keys/{key}` | 读取该 key 的全部存活版本（一个或多个兄弟）。 |
| `POST /replicas/{id}/keys/{key}/merge` | **显式合并**：`{"value":"...","context":["A-1","B-1"]}`。新版本**仅派生自所列上下文版本**（Dynamo 风格客户端上下文）：时钟 = join(各上下文时钟) 后本副本分量 +1，因果覆盖上下文版本；副本虽见过、但客户端**未列入上下文**的并发版本继续保留。`context` 不能为空（整体覆盖请用 PUT）。 |
| `POST /replicas/{id}/keys/{key}/messages` | **显式投递一条消息**（模拟网络包）：`{"versions":[Version,...]}`。每个版本返回 `accepted` / `duplicate` / `superseded`。可乱序、可重复、可迟到。 |

### 4.3 同步

```
POST /replicas/{id}/sync
{
  "from": "A", "to": "B",
  "mode": "two-way"          // "one-way"（默认）或 "two-way"
}
```

- `one-way`：把 `from` 的全量状态合并进 `to`（反熵传播的一个方向）；
- `two-way`：双向交换。两个方向的合并是可结合、可交换的集合求反链运算，
  在同一把锁下完成，**结束后双方对每个 key 持有的版本集合完全一致（收敛）**。

返回每个 key 的计数：`accepted`（新收录）、`duplicate`（id 已存在）、
`superseded`（因果上更旧被丢弃）、`versions_after`（合并后存活版本数）。

### 错误码

- `400`：JSON 非法、出现未知字段、合并上下文为空/版本不存在、版本字段非法等；
- `404`：副本或 key 不存在；
- `409`：创建重名副本。
错误体统一为 `{"error":"..."}`。

---

## 5. 请求样例（curl）

```bash
# 两个网络隔离的副本
curl -s -X POST localhost:18080/replicas -d '{"id":"A"}'
curl -s -X POST localhost:18080/replicas -d '{"id":"B"}'

# 隔离期间各自写同一个 key（并发写）
curl -s -X PUT localhost:18080/replicas/A/keys/cfg -d '{"value":"from-A"}'
curl -s -X PUT localhost:18080/replicas/B/keys/cfg -d '{"value":"from-B"}'

# 重复投递同一条消息：第一次 accepted，之后全部 duplicate（幂等）
curl -s -X POST localhost:18080/replicas/B/keys/cfg/messages \
  -d '{"versions":[{"id":"A-1","origin":"A","clock":{"A":1},"value":"from-A"}]}'

# 分区恢复，双向同步 -> 双方都持有 A-1 与 B-1 两个并发版本
curl -s -X POST localhost:18080/replicas/A/sync \
  -d '{"from":"A","to":"B","mode":"two-way"}'

# 读取兄弟版本
curl -s localhost:18080/replicas/A/keys/cfg

# 带上下文的显式合并：客户端解决冲突，新时钟覆盖 A-1 和 B-1
curl -s -X POST localhost:18080/replicas/A/keys/cfg/merge \
  -d '{"value":"resolved=A+B","context":["A-1","B-1"]}'

# 合并传播到 B
curl -s -X POST localhost:18080/replicas/A/sync \
  -d '{"from":"A","to":"B","mode":"one-way"}'

# 迟到的合并前旧写 B-1 到达：superseded，不会复活旧版本
curl -s -X POST localhost:18080/replicas/B/keys/cfg/messages \
  -d '{"versions":[{"id":"B-1","origin":"B","clock":{"B":1},"value":"from-B"}]}'
```

---

## 6. 正确性依据（简要）

每个 key 的版本集合始终维护为向量时钟偏序下的**极大反链（antichain）**：

- 新写入与当前每个存活版本比较：
  - 被某个存活版本支配（before/equal）→ `superseded` 丢弃；
  - 支配若干存活版本 → 删掉被支配者，收录新版本（因果覆盖）；
  - 与所有存活版本并发 → 作为兄弟收录。
- 该运算（在版本集合上求极大反链并按 id 去重）满足**结合律、交换律、幂等律**，
  因此消息乱序、重复、同步方向如何安排，最终只取决于"见过的版本集合"——这就是收敛性。
- 合并操作的时钟是 `join(上下文版本时钟…)` 后本地分量 +1（版本 id 的本地序号仍从
  副本级事件计数器分配，保证唯一）。它严格支配每个上下文版本，而对未列入上下文的
  并发版本无可比关系，后者自然保留——这要求客户端显式承担"我解决了哪些兄弟"的责任，
  服务端不会替它删除未声明的版本。

不变量：副本时钟始终支配其存储的每个版本时钟；同一 id 的版本全局只存一份。

## 7. 源码结构

| 文件 | 内容 |
| --- | --- |
| `clock.go` | 向量时钟类型、偏序比较、分量最大值 join、克隆 |
| `clockorder.go` | 比较结果枚举（equal/before/after/concurrent） |
| `version.go` | 版本类型、投递结果、核心反链合并 `MergeVersions` |
| `store.go` | 副本/存储：本地写、显式合并、消息投递、快照、单向/双向同步 |
| `server.go` | `net/http` 路由、JSON 编解码、错误码映射 |
| `main.go` | 进程入口与 flag |
| `*_test.go` | 时钟/反链单元测试、存储级验收测试、HTTP 端到端验收测试、并发压力测试 |
| `examples/demo.sh` | 真实 HTTP 验收演示脚本 |
| `RUNLOG.md` | 实际运行测试与示例的结果记录 |
