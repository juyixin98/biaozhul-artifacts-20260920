# alertfsm — 告警迟滞状态机（纯后端样例）

用 Go 实现的可观测性数据处理后端：通过 HTTP 摄入合成指标样本，按阈值规则用
**虚拟时钟**评估状态机，支持**持续触发**、**恢复迟滞**和**缺数状态**，状态持久化到
本地 JSON 快照，通知事件只在状态转换时产生。不依赖真实监控平台或外部数据库，
不含前端。

## 状态机

```text
              breach 样本                     持续越限 ≥ pending_for
   ok ───────────────────────► pending ───────────────────────► alerting
    ▲                             │ 好样本                          │
    │ 持续恢复 ≥ recovery_for      ▼                                 │ 好样本
    └─────────── ok ◄──── recovering ◄──────────────────────────────┘
                  ▲                      │ 恢复期间再次越限（不产生新通知）
                  └──────────────────────┘

   任何有数据状态 ──超过 no_data_for 无样本──► no_data        [no_data_start]
   no_data ──好样本──► ok；no_data ──越限样本──► pending       [no_data_end]
```

- `pending → alerting` 产生 `firing`；`recovering → ok` 产生 `resolved`。
- `alerting` 与 `recovering` 之间来回抖动**不**产生事件（已经通知过）。
- 进入 `pending` / `recovering` 本身也**不**产生事件——只有最终转换产生。

## 关键语义（验收要点）

| 主题 | 行为 |
|---|---|
| 持续触发 | 值越限后进入 `pending`，从第一个越限样本起持续 `pending_for` 才 `firing`；期间任意一个好样本都会取消计时。 |
| 恢复迟滞 | 告警中收到好样本进入 `recovering`，持续 `recovery_for` 才 `resolved`；期间再次越限立即回到 `alerting`，不重复通知。 |
| 缺数状态 | 距最后一条样本超过 `no_data_for` 进入 `no_data`（产生 `no_data_start`）；恢复数据时按新样本值进入 `ok`/`pending`（产生 `no_data_end`）。`no_data_for=0` 可关闭。 |
| 虚拟时钟 | 时间只随样本时间戳和 `POST /api/v1/admin/tick` **单调递增**；倒退返回 400。`pending_for=0` 表示越限样本到达即触发。 |
| 重复样本 | 同一 `metric + ts_ms` 重复摄入幂等（首次值为准），分类为 `duplicate`，**不推进任何时长**、不更新最后值。 |
| 时间倒序 | 早于已评估水位的样本分类为 `late`：照常存储可查询，但**不**驱动状态机。 |
| 配置修改 | `PUT` 规则会把状态**明确重置**为 `ok` 并产生 `reset` 事件（新阈值下旧持续时长无意义）。 |
| 通知 | 事件**仅**在状态转换时产生，可通过 `GET /api/v1/events` 轮询（支持 `after_id` 增量、`rule_id` 过滤）。 |

边界采用"步长"语义：恰好在 `越限起点 + pending_for`（及 `+ no_data_for`）的
时刻满足条件，这与评估时刻已知最后一个值仍越限的语义一致。

## 运行

要求 Go 1.22+。

```bash
go build ./...
go run ./cmd/alertfsm -addr :8080 -data ./data
# 全新数据库的虚拟时钟种子（毫秒），默认使用固定值 1700000000000 以保证可复现：
#   -base-clock-ms 1700000000000
```

每次规则变更、摄入、tick 都会原子写入 `data/snapshot.json`（临时文件 + rename），
重启后规则、状态、事件和样本自动恢复。

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查，含当前虚拟时钟 |
| GET | `/api/v1/clock` | 查询虚拟时钟 |
| POST | `/api/v1/admin/tick` | 推进时钟：`{"to_ms":…}` 或 `{"by":"30s"}`/`{"by_ms":…}` |
| POST | `/api/v1/rules` | 创建规则 |
| GET | `/api/v1/rules` / `/api/v1/rules/{id}` | 列出/查看规则 |
| PUT | `/api/v1/rules/{id}` | 修改配置（**重置状态**，产生 reset 事件） |
| DELETE | `/api/v1/rules/{id}` | 删除规则及其状态（历史事件保留） |
| POST | `/api/v1/ingest` | 批量摄入样本 `{"samples":[{metric,ts_ms,value}]}` |
| GET | `/api/v1/samples?metric=…[&from_ms&to_ms]` | 查询样本（含被忽略的倒序样本） |
| GET | `/api/v1/states` / `/api/v1/rules/{id}/state` | 查询全部/单条状态 |
| GET | `/api/v1/events[?rule_id&after_id&limit]` | 查询通知事件 |

完整 curl 样例见 [`examples/requests.md`](examples/requests.md)。
一条覆盖全部场景的合成数据演练脚本：

```bash
bash examples/demo.sh
```

## 规则字段

```json
{
  "id": "cpu-high",
  "metric": "cpu.usage",
  "threshold": 80,
  "direction": "above",
  "pending_for": "60s",
  "recovery_for": "30s",
  "no_data_for": "120s"
}
```

- `direction`：`above`（值 > 阈值越限）或 `below`（值 < 阈值越限）。
- 三个时长字段接受毫秒数字或 `"60s"`/`"2m"` 这类字符串。

## 项目结构

```text
cmd/alertfsm/        程序入口（HTTP 服务、优雅退出、落盘）
internal/model/      规则、状态、样本、事件类型与时长 JSON 解析
internal/store/      内存存储 + 本地 JSON 快照（去重、有序样本、事件序列）
internal/engine/     状态机核心：虚拟时钟推进、迟滞/缺数判定、转换事件
internal/api/        HTTP handler
examples/            curl 请求样例与端到端 demo 脚本
```

## 测试

```bash
go test ./...                 # 单元 + HTTP 端到端
go test -race ./...           # 竞态检测
go test -cover ./...          # 覆盖率
```

测试覆盖：完整 firing/resolved 生命周期、阈值附近抖动（pending 取消与
recovering 翻回）、从 ok/pending/alerting 三种状态进入缺数及恢复、
长时间缺数不重复通知、倒序样本只存储不评估、重复样本不累加时长、
配置修改重置、乱序批次按时间戳处理、零时长立即触发、below 方向、
快照重启恢复等。

## 设计说明与局限

- 这是教学/样例实现：单实例、全内存 + JSON 快照、一把互斥锁；没有做
  水平扩展、分片、保留策略（样本每指标保留最近 10000 条）或推送式通知通道
  （事件通过轮询 API 获取，便于演示"只在转换时产生"）。
- 多个规则可监听同一指标；样本与 tick 共用一个全局虚拟时钟。
