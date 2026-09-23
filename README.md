# OR-Set 观察删除集合（Observed-Remove Set）复制服务

纯后端 Go 服务，仅用标准库 `net/http`，无任何第三方依赖。
实现 OR-Set CRDT：增加时生成全局唯一标签，删除只移除「已观察到」的标签，
支持离线副本、消息任意重排与重复投递，最终状态必然收敛。

## 依赖与启动

- 依赖：Go ≥ 1.22（使用了 `http.ServeMux` 的方法路由，如 `POST /add`）。无第三方模块，`go.mod` 即锁定文件（无 `go.sum` 因为没有外部依赖）。
- 运行测试：`go test -race -v ./...`
- 启动副本（每个副本一个进程，`-id` 必须全局唯一）：

```sh
go run . -id node-1 -addr :8001
go run . -id node-2 -addr :8002
go run . -id node-3 -addr :8003
```

也可以 `go build -o orset .` 后直接运行二进制。

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/add` | body `{"element":"x"}`，返回 `{"element":"x","tag":"node-1#3"}` |
| POST | `/remove` | body `{"element":"x"}`，把当前已观察到的该元素全部标签移入墓碑，返回被移除的标签 |
| GET | `/elements` | 返回当前存活元素列表 `{"elements":[...]}` |
| GET | `/state` | 返回完整 CRDT 状态（adds + 墓碑 rems），用于副本间同步 |
| POST | `/merge` | body 为 `/state` 的 JSON，合并进本副本（幂等，可重复/乱序推送） |
| POST | `/sync` | body `{"peer":"http://localhost:8002"}`，与对端双向同步（先拉后推） |
| GET | `/healthz` | 健康检查 |

## 请求样例

```sh
# 三个副本上分别写入
curl -s -XPOST localhost:8001/add -d '{"element":"apple"}'
# {"element":"apple","tag":"node-1#1"}
curl -s -XPOST localhost:8002/add -d '{"element":"banana"}'
curl -s -XPOST localhost:8003/add -d '{"element":"apple"}'   # 并发同元素，标签不同

# 删除：只移除本副本已观察到的标签
curl -s -XPOST localhost:8001/remove -d '{"element":"apple"}'
# {"element":"apple","removed_tags":["node-1#1"]}

# 两两同步（顺序任意、可重复）
curl -s -XPOST localhost:8001/sync -d '{"peer":"http://localhost:8002"}'
curl -s -XPOST localhost:8002/sync -d '{"peer":"http://localhost:8003"}'
curl -s -XPOST localhost:8001/sync -d '{"peer":"http://localhost:8003"}'

# 三个副本收敛到同一集合；node-3 并发 add 的 apple 未被 node-1 观察到，仍然存活
curl -s localhost:8001/elements
# {"elements":["apple","banana"]}
```

一键演示（三副本、分区、恢复、收敛）：`sh examples/demo.sh`

## 设计说明

### 为什么删除只移除「已观察」标签

每次 `Add` 生成全局唯一标签 `副本ID#序号`。`Remove(e)` 只把本副本当前
`adds[e]` 里的标签复制到墓碑集合 `rems[e]`。若另一副本并发 `Add` 了同一元素，
其标签不在本次删除的观察范围内，合并后该标签仍然存活 —— 元素「复活」，
即 add-wins 语义。这是 OR-Set 的核心：删除不能误伤自己从未见过的并发写入。

### 为什么支持离线副本与任意消息重排

合并（`Merge`）是对 adds、rems 两个集合分别取**并集**。并集满足：

- **交换律**：消息乱序到达结果相同；
- **结合律**：分批合并与一次合并结果相同；
- **幂等律**：同一消息重复投递结果不变。

因此副本可以离线任意长时间，恢复后以任意顺序、任意次数交换状态，
都收敛到同一不动点（测试 `TestPermutationConvergence` 穷举 6! = 720 种
投递顺序逐一验证）。

### 墓碑（tombstone）何时可以安全回收

墓碑不能随意删除：若副本 R 离线期间，其他副本回收了标签 t 的墓碑，
而 R 重新上线时推送的旧状态里仍含 t 的 add 记录，合并后 t 会因「无墓碑」
而复活 —— 已删除的元素幽灵回归。

**安全回收的充要条件：集群中每一个副本都已经合并了包含该墓碑的状态。**
工程上的常见判定方式：

1. **版本向量确认**：每个副本维护版本向量，反熵（anti-entropy）时互相交换。
   当本副本确认所有已知副本的版本向量都越过了产生该墓碑的删除操作时，
   该墓碑可回收。
2. **全量同步轮次确认**：运维上保证所有副本完成过一轮全互联同步
   （本服务的 `/sync` 跑通全网格）之后，由协调者发起一次 GC。
3. **离线副本超时剔除**：对超过 TTL 未上线的副本先做成员剔除
   （其后续写入需走重新加入流程、重新全量引导），再回收墓碑。

本实现**未做自动回收**（见「未完成项」），墓碑随状态永久保存，
换取任意时长离线的正确性。

## 测试

`go test -race -v ./...`，覆盖验收要求：

| 测试 | 验证点 |
|---|---|
| `TestAddGeneratesUniqueTags` | 标签全局唯一 |
| `TestRemoveOnlyObservedTags` | 删除只影响已观察标签，并发 add 存活 |
| `TestConcurrentAddRemove` / `TestHTTPConcurrentOps` | 并发增加删除（`-race` 下无数据竞争） |
| `TestDuplicateSyncIdempotent` | 重复同步幂等 |
| `TestThreeReplicaPartitionMerge` / `TestHTTPThreeReplicaPartitionHeal` | 三副本分区合并（内存态 + 真实 HTTP 两个层面） |
| `TestPermutationConvergence` | 排列测试：720 种消息投递顺序 + 重复投递全部收敛 |
| `TestRemoveDeliveredBeforeAdd` | 删除先于增加到达的乱序极端情形 |

## 未完成项 / 已知限制

- **墓碑自动回收未实现**：需要版本向量 + 成员管理，当前墓碑永久保留，
  长期运行状态会单调增长。
- **同步为全量状态推送**：未做增量（delta）同步，数据量大时带宽浪费。
- **无持久化**：重启即丢失状态，重新加入需从其他副本全量引导。
- **无认证/鉴权**：仅限可信网络内使用。
