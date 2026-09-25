# tracestitch — 分布式追踪拼装（纯后端样例）

用 Go 实现的可观测性数据处理后端：通过 HTTP 接收乱序到达的 span，按 `traceId`
拼装成调用树，输出不可变修订历史，并提供本地 WAL + 快照持久化样例。全部数据
为合成数据，不依赖任何真实监控平台；不包含前端。

## 要解决的问题

分布式追踪里，span 经网络上报后**到达顺序不确定**，需要在服务端完成：

1. **按 traceId 拼装乱序 span**，容忍「子先父后」（父 span 晚到时自动挂接）。
2. **识别重复与冲突**：相同 spanId 的完全重发幂等去重；载荷冲突时**首条为准**并记录差异。
3. **缺根超时**：根迟迟不到，超过超时时间后输出带**不完整标志**的修订；
   根**迟到**后产生新修订并补全。
4. **检测父链循环**（自环、多节点环、汇入环的 feeder 节点）。
5. **跨服务时钟偏差**只告警，不影响因果结构。

## 核心设计原则：不拿壁钟推断因果

> **因果关系只来自 span 引用（`parentSpanId`），从不使用时间戳排序或判定父子。**

- `startUnixNano/endUnixNano` 是各服务本地钟的读数，跨服务可能偏差几秒，
  用它们排因果必然出错。因此树的挂载、根的判定、完整性判定全部基于引用图。
- 时间戳只用于一件事：在父子引用边完整时，检测
  `child.start < parent.start` / `child.end > parent.end`（超过容差）并产生
  `clockSkew` 告警。即使告警存在，trace 结构与完整性仍按引用计算。
- 「超时」是另一个时间概念，由**可注入的 Clock**驱动：
  - 生产用系统壁钟，后台 sweeper 周期性密封；
  - 测试用 `clock.Fake` 手动拨钟 + 显式 `Sweep()`，**没有任何真实 sleep**，
    也不通过壁钟先后顺序断言因果。

### 修订（revision）与包含关系

每次实质状态变化（新 span、冲突、超时、迟到）都追加一个**不可变修订**：

```
v1 ⊆ v2 ⊆ ... ⊆ vn      （spanId 集合单调包含，绝不丢 span）
```

`GET /v1/traces/{id}/containment` 与 `Assembler.CheckRevisionContainment`
逐版核对该不变量。冲突/超时修订的 spanId 集合与上一版相同（集合相等，仍满足包含）。

### 完整性（complete）定义

一个 trace 完整，当且仅当：**恰好一个根**（`parentSpanId == ""`）、
**无孤儿**（引用的父都在 trace 内）、**无环**。时钟偏差不影响完整性。

## 目录结构

```
cmd/tracestitch/          HTTP 服务入口（WAL 重放 + sweeper + 优雅退出）
internal/
  model/                  span / 修订 / 视图数据模型
  clock/                  Clock 接口、系统钟、测试用 Fake 钟
  assembler/              核心：乱序拼装、去重冲突、环检测、修订、包含校验
  storage/                本地持久化：data/wal.jsonl + data/snapshots/*.json
  server/                 HTTP 路由与处理器
examples/                 合成请求样例 + demo.sh 一键演示
```

## 快速开始

需要 Go 1.22+。

```bash
go test ./...                 # 自动化测试（也可加 -race）
go run ./cmd/tracestitch      # 默认监听 127.0.0.1:8080，数据写入 ./data

# 一键端到端演示（自动选空闲端口、临时数据目录，退出自动清理）
./examples/demo.sh
```

服务参数：

| flag | 默认值 | 含义 |
|---|---|---|
| `-addr` | `127.0.0.1:8080` | 监听地址 |
| `-data-dir` | `.` | 数据根目录（WAL/快照在其 `data/` 下） |
| `-trace-timeout` | `30s` | 不完整 trace 超时密封时间 |
| `-skew-tolerance` | `1ms` | 父子壁钟嵌套容差，超出即告警 |
| `-sweep-interval` | `2s` | 后台超时扫描周期 |

## HTTP API

| 方法与路径 | 说明 |
|---|---|
| `POST /v1/spans` | 批量摄入 `{"spans":[...]}` |
| `POST /v1/span` | 摄入单个 span |
| `GET /v1/traces` | 列出全部 trace 视图 |
| `GET /v1/traces/{id}` | 拼装视图：forest、conflicts、全部修订 |
| `GET /v1/traces/{id}/revisions` | 修订历史 |
| `GET /v1/traces/{id}/revisions/{v}` | 指定修订（v 从 1 开始） |
| `GET /v1/traces/{id}/containment` | 核对每版 spanId 包含关系 |
| `POST /admin/sweep` | 立即执行一次超时扫描（壁钟判定） |
| `POST /admin/traces/{id}/flush` | **确定性**强制密封单个缺根 trace（demo/测试用，不等待） |
| `GET /healthz` | 健康检查 |

摄入结果 `status`：`accepted` / `duplicate`（完全相同重发，幂等无新修订）/
`conflict`（同 id 不同载荷，首条为准，记录 `differingFields`）/ `invalid`。

span 字段：

```json
{
  "traceId": "T1",
  "spanId": "a",
  "parentSpanId": "",
  "serviceName": "gateway",
  "name": "GET /x",
  "startUnixNano": 1727172000100000000,
  "endUnixNano":   1727172000950000000,
  "attributes": {"region": "cn-north-1"}
}
```

典型调用流程见 [`examples/API.md`](examples/API.md)，合成载荷见 `examples/0*.json`。

## 持久化与崩溃恢复

- **WAL（`data/wal.jsonl`）**：每个被接受的 span 与每次超时密封都是一条
  JSONL 事件，携带单调递增 `ingestSeq` 与接收时刻；写入后 `flush + fsync`。
  先落盘再改内存，已确认的摄入不会丢。
- **快照（`data/snapshots/{traceId}.json`）**：每次修订后原子替换
  （写临时文件 + rename）当前完整视图，便于直接查看。
- **重启**：启动时回放整个 WAL。修订版本号、原因（initial/extended/conflict/
  timeout/late）、密封状态都按日志确定性重建——这由
  `internal/storage/filestore_test.go` 的跨实例重放测试覆盖。

## 关键算法

- **乱序挂接**：span 以 `map[spanId]span` 存储，另建「父→子」邻接；每次摄入
  全量重算分析，父晚到时子自动从「孤儿」变为正常节点。无状态扫描，避免增量修补漏洞。
- **环检测**：父关系是函数图（每点至多一个父），沿父链做三色遍历：
  遇到栈上节点即定位环；环上节点与「汇入该环的 feeder」全部标 `inCycle`；
  规范环路径以环上最小 id 为起点并首尾闭合，保证输出确定。
- **森林渲染**：规范根在前，孤儿子树、环片段在后，保证每个 span 恰好出现一次；
  环上的回边不会导致无限递归（节点放置一次后即跳过）。
- **确定性**：所有集合输出按 id 排序；摄入序号单调；测试不依赖真实时序。

## 验收点与测试对照

| 验收要求 | 覆盖位置 |
|---|---|
| 乱序、子先父后 | `TestOutOfOrderChildBeforeParent`、`TestHTTPEndToEndAssembly` |
| 缺根 + 超时不完整标志 | `TestMissingRootTimeoutThenLateCompletion`、`TestWALReplayRebuildsRevisions` |
| 迟到补全生成新修订 | 同上（断言 reason=`late`、新版 complete） |
| 重复 span（幂等 + 冲突首条为准） | `TestDuplicateAndConflict` |
| 跨服务时钟偏差、不用壁钟断因果 | `TestCrossServiceClockSkewDoesNotChangeCausality`、`TestClockSkewOverHTTP` |
| 父链循环（自环/多节点环/feeder） | `TestParentChainCycles` |
| 每版包含关系核对 | 每个用例内调用 `CheckRevisionContainment` + HTTP containment 断言 |
| 崩溃恢复（WAL 重放一致） | `TestWALReplayRebuildsRevisions` |

运行记录见 [`RUNLOG.md`](RUNLOG.md)。

## 范围与非目标

- 纯后端 + 合成数据样例：无鉴权、无分布式多副本、无限流、无前端。
- 快照是查看样例而非缓存层；恢复以 WAL 为准。
- 时间戳不做 NTP 校准时钟同步，只做超容差告警。
