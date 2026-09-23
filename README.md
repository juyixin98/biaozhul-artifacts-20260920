# metricsink — 指标保留层压缩（纯后端样例）

用 Go 实现的可观测性数据处理后端：HTTP 摄入 / 查询 + 本地持久化，
数据全部为本地生成的合成数据，不依赖任何真实监控平台、无前端。

核心能力：把原始样本**增量降采样**为 **1 分钟层**与 **1 小时层**，
每层保存四个充分统计量 `count / sum / min / max`（均值仅查询时由
`sum/count` 派生）。高层桶永远由低层桶合并而来，
**绝不做"平均值的平均"**；迟到数据以"同一样本 ID 重新写入"的形式修订，
修订效果可沿分钟层向小时层传播。

---

## 1. 为什么不能只存平均值

两个分钟桶样本数悬殊时，小时均值 ≠ 分钟均值的简单平均：

| 分钟桶 | 样本数 | 均值 |
|---|---:|---:|
| A | 1 | 100 |
| B | 99 | 2 |

- 错误（均值再平均）：`(100 + 2) / 2 = 51`
- 正确（用 count/sum 合并）：`(100·1 + 2·99) / (1+99) = 2.98`

`internal/agg/bucket.go` 的 `Merge` 做的是 `count 相加、sum 相加、
min 取小、max 取大`。对应回归测试：
`internal/agg/bucket_test.go::TestMergeNotAverageOfAverages`。

## 2. 三层结构与增量算法

```
原始样本 (raw, 按幂等 id 存放)
   │  写入时增量 Add
   ▼
分钟桶 (60s):  count / sum / min / max
   │  每批写入结束后，用该小时覆盖的 ≤60 个分钟桶 Merge 折叠
   ▼
小时桶 (3600s): count / sum / min / max
```

- **纯新增**：样本 `Add` 进对应分钟桶（O(1)）；脏小时桶在批次末尾
  由分钟桶折叠（每个脏小时固定 60 次 `Merge`，与样本量无关）。
- **迟到修订**（同 ID、值或时间或标签变化）：摘掉旧样本、放入新样本，
  受影响分钟桶用**桶内剩余原始样本重建**（count/sum/min/max 全部重算），
  再由分钟桶重新折叠父小时桶。样本可跨分钟、跨小时、甚至跨序列移动，
  旧位置与新位置都会被修正。
- **删除**（`POST /v1/delete`）：与修订同路径的显式订正，传播移除效果；
  删掉桶内唯一样本会在该层留下一个真实空洞。
- **空桶**：无数据的时间不存键；查询带 `fill=zero` 时以
  `count=0, avg=null` 显式补齐，空桶因此可观测。

不变量：**任意小时桶恒等于其 60 个分钟桶的折叠**
（测试 `TestHourAlwaysFoldedFromMinutes` 在多轮修订/删除后逐小时校验）。

## 3. 明确不可恢复的细节（有损点）

分钟/小时桶只保存四个充分统计量，以下信息一旦离开原始样本即**永久丢失**：

1. **桶内分布形态**：无法求分位数/中位数/众数、无法还原方差以外的矩、
   无法区分 `[0,100]` 与 `[49,51]`（两者 count/sum/min/max 可能相同）。
2. **去重计数 / 基数**：`count` 是样本条数，不是唯一值个数。
3. **样本到达时间、来源等元信息**（本样例未采集）。
4. **原始保留窗口驱逐之后**（`POST /v1/evict`）：
   - 分钟/小时聚合桶原样保留（压缩成果还在）；
   - 被驱逐原始样本**不能再参与精确重算**，对旧桶的迟到修订/删除
     **无法精确传播**，因此被驱逐 ID 一律拒绝再写入（ID 不可复用）；
   - 此时对旧时间段调 `source=recompute` 会得到空桶——这正是
     "聚合仍可查、但细节不可恢复"的直接证据（见下方实测第 6 步）。

> 删除（delete）在原始样本尚在时是**精确**的（用剩余原始样本重建）；
> 驱逐（evict）之后则不再有精确删除的可能。这是刻意的保留/精度权衡。

## 4. HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET  | `/healthz` | 健康检查 |
| POST | `/v1/ingest` | 批量摄入/修订样本（幂等 ID） |
| GET  | `/v1/query` | 查询原始样本或分钟/小时层 |
| GET  | `/v1/series` | 列出序列，支持 `label.k=v` 过滤 |
| POST | `/v1/delete` | 按 ID 删除样本并传播 |
| POST | `/v1/evict` | 驱逐早于指定时刻的原始样本（有损） |
| POST | `/v1/snapshot` | 立即落快照 |

查询参数：

- `metric`（必填）、`label.<k>=<v>`（可多个，等值匹配）
- `start` / `end`：Unix 秒或 RFC3339，闭区间
- `step`：`raw` / `60`(`1m`) / `3600`(`1h`) / `auto`（默认，按时间跨度选层）
- `fill=zero`：补齐空桶为 `count=0, avg=null`
- `source=recompute`：**不看存储层**，直接从原始样本重算，用于对账

每个点返回 `{ts,count,sum,min,max,avg}`；`avg` 仅在 `count>0` 时出现。
完整请求样例见 [`examples/requests.http`](examples/requests.http)。

### 持久化

数据目录下两个文件，启动时按 `snapshot.json` + 重放 `oplog.jsonl` 恢复：

- `oplog.jsonl`：每条摄入/删除/驱逐操作 **append + fsync**，
  先落盘成功才改内存（崩溃不丢已确认写入）；
- `snapshot.json`：定时（默认 60s）或退出/手动触发的完整状态快照，
  原子 rename 提交，快照后截断 oplog；
- 重放后用原始样本重建受影响分钟桶、并**从分钟桶重建全部小时桶**，
  保证恢复结果与正常运行一致。

## 5. 如何运行

要求 Go 1.22+（开发实测 go1.22.2，仅用标准库）。

```bash
# 构建并启动
go run ./cmd/server -addr=:8080 -data=./data

# 另开终端：播种合成数据（可选，方便观察；也可直接 POST /v1/ingest）
go run ./cmd/seed -density=ragged -hours=3 -seed=42
# density: dense(10s/点) sparse(90s/点) ragged(5~200s 不规则) gappy(中间整段缺口)
```

一键端到端演示（自构建、自启临时服务、播种、修订、删除、空桶、驱逐、重启）：

```bash
bash examples/scenario.sh           # 可用 PORT=xxxx 改端口
```

只跑"存储层 vs 原始重算"对账（需要服务已播种）：

```bash
bash examples/reconcile.sh http://127.0.0.1:8080
```

## 6. 实测记录（2026-09-24，go1.22.2/linux）

命令与输出归档在 `examples/output/`（`03-scenario.txt` 为完整运行记录）。
以下为 `bash examples/scenario.sh`（PORT=18349）的实际结果摘录：

**四种密度 × 两层，与原始重算比对（count 精确相等，sum 容差 1e-9，min/max 精确相等）：**

```
host=dense   分钟层 桶数=180 空桶=  0 与原始重算一致(eps=1e-9): True
host=dense   小时层 桶数=  3 空桶=  0 与原始重算一致(eps=1e-9): True
host=sparse  分钟层 桶数=180 空桶= 60 与原始重算一致(eps=1e-9): True
host=sparse  小时层 桶数=  3 空桶=  0 与原始重算一致(eps=1e-9): True
host=ragged  分钟层 桶数=180 空桶= 88 与原始重算一致(eps=1e-9): True
host=ragged  小时层 桶数=  3 空桶=  0 与原始重算一致(eps=1e-9): True
host=gappy   分钟层 桶数=180 空桶= 60 与原始重算一致(eps=1e-9): True
host=gappy   小时层 桶数=  3 空桶=  1 与原始重算一致(eps=1e-9): True
总体: 全部一致 ✅
```

覆盖到的验收点：

- **跨层边界**：样本落在 `xx:59:59 / xx+1:00:00` 两侧归属正确
  （`TestCrossLayerBoundary`）；修订把样本移到别的分钟/小时后旧桶扣减、
  新桶增加（`TestLateRevisionMovesAcrossBoundaries`）。
- **空桶**：sparse/ragged/gappy 均出现空分钟桶，gappy 出现空小时桶；
  `fill=zero` 显式可见，不补齐时稀疏返回。
- **历史修订**：同 ID 改值后分钟桶与小时桶同步更新且与重算一致；
  实测改 ragged 某点为 500，分钟/小时 `max` 同步变 500，删除后回滚。
- **不同样本密度**：dense/sparse/ragged/gappy 全部对账一致。

**驱逐的有损性实测：**

```
存储层 17点小时桶: count=360 sum=18243.117700 min=27 max=77   # 聚合还在
原始重算 17点小时: count=0 sum=0.000000 min=0 max=0            # 原始已不可恢复
被驱逐 ID 的迟到修订 -> rejected = 1
```

**持久化**：杀进程后用同一数据目录重启，`/healthz` 正常，
18 点小时桶 `count=360` 等聚合完整恢复；快照恢复与"仅 oplog 崩溃重放"
均有测试覆盖（`TestPersistenceSnapshot`、`TestPersistenceOplogReplay`）。

## 7. 自动化测试

```bash
go test ./...            # 全部 25 个测试函数（含子测试共 30+ 断言单元）
go test -race ./...      # 竞态检测（已通过）
go test -v ./... | grep -E '^(---|=== RUN)'   # 查看每个用例
```

测试分层：

- `internal/agg`：Merge 语义、空桶、**均值再平均反例**；
- `internal/model`：时间解析（含 RFC3339 不被前缀误解析的回归）、桶对齐、序列键；
- `internal/store`：四密度对账、跨边界、桶内/跨桶/跨小时/跨序列修订、
  删除传播、幂等乱序批次、驱逐有损性、快照/oplog/快照+oplog 恢复、
  小时恒等于分钟折叠不变量、非法输入拒绝；
- `internal/api`：HTTP 全链路摄入→查询→`source=recompute` 对账、
  修订/删除传播、400/409 错误、标签选择器。

> 开发过程中由测试抓出并修复的两个真实问题：
> ① 无快照仅重放 oplog 时小时层无法建立（小时键需从分钟层派生）；
> ② `ParseTs` 用前缀扫描把 RFC3339 字符串 `"2025-…"` 误解析成 Unix 秒 2025。

## 8. 工程边界（明确不做）

- 单进程内存存储 + 本地文件，**无**分布式复制/分片/多租户鉴权；
- 仅两个固定降采样层（1m / 1h），不做任意窗口在线聚合
  （查询对其他步长会路由到容纳它的已存层）；
- 不做 TTL 自动执行（驱逐由接口显式触发）；不做查询侧下推/列式编码；
- 无前端、无图表。

## 9. 目录结构

```
.
├── cmd/server/main.go        # 服务入口（HTTP + 优雅退出 + 周期快照）
├── cmd/seed/main.go          # 合成数据播种工具
├── internal/
│   ├── agg/bucket.go         # count/sum/min/max 聚合原语与 Merge
│   ├── model/model.go        # Sample/Point/Query、时间对齐与序列键
│   ├── store/store.go        # 三层存储、修订传播、删除、驱逐、WAL+快照
│   ├── synth/synth.go        # 确定性合成数据（四种密度）
│   └── api/server.go         # HTTP handler
└── examples/
    ├── requests.http         # 全部接口的请求样例
    ├── reconcile.sh          # 存储层 vs 原始重算对账脚本
    ├── scenario.sh           # 一键端到端演示
    └── output/               # 实际运行输出归档
```
