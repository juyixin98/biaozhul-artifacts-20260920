# tztrigger — 带时区的定时触发器服务

纯后端 Go 服务（仅标准库 `net/http`，**零第三方依赖**）：按"分钟 / 小时 / 周几 +
IANA 时区"表达式注册定时触发器，明确处理夏令时（DST）缺失/重复时刻，
按逻辑触发 ID 去重，支持长时间停机后的补触发上限。

## 快速开始

依赖：**Go 1.23+**（开发验证用 go1.23.4），无其他依赖。

```bash
go build -o tztrigger .   # 或 go run .
./tztrigger               # 默认监听 :8080，PORT 环境变量可覆盖
```

启动日志会打印内嵌 tz 数据库版本与 SHA-256：

```
tztrigger 启动：监听 :8080，内嵌 IANA tz 数据库 2024a（zoneinfo.zip sha256 2359012e2cf90a23…）
```

请求样例见 [`examples/requests.sh`](examples/requests.sh)（`BASE=http://localhost:8080 bash examples/requests.sh`）。

## 表达式语法

三个字段，之间为"与"关系（同时命中才触发）：

| 字段 | 取值 | 说明 |
|---|---|---|
| `minutes` | 0–59 | 必填 |
| `hours` | 0–23 | 必填 |
| `weekdays` | 0–6（0=周日，7 也视为周日） | 必填 |

每个字段支持 cron 风格子集：`*`、`5`、`1,2,3`、`1-5`、`*/10`、`1-30/5`、`5/20`，
以及逗号组合，如 `"0,30"`、`"9-18/2"`。不支持秒、日期、月份字段，也不支持
`@daily`、名称（`MON`）等扩展。

## 夏令时（DST）语义

对表达式命中的每一个**本地墙钟时刻**：

- **缺失时刻**（春季拨快产生的空隙，如 America/New_York 2026-03-08 02:30）：
  该时刻在本地不存在，**跳过，不触发**。
- **重复时刻**（秋季拨回产生的重叠，如 America/New_York 2026-11-01 01:30 出现两次）：
  **只在较早的一次触发**（05:30 UTC，EDT 侧），较晚的一次（06:30 UTC，EST 侧）不触发。
- 非整小时偏移切换（如 Australia/Lord_Howe 的 30 分钟 DST）同样适用，
  有专门测试覆盖。

实现原理（`internal/schedule/resolveLocal`）：墙钟时刻 W 对应的真实瞬间必为
W−off，off 是 W 附近生效的某个 UTC 偏移。取 W 前后 ±3h 内出现过的全部偏移构造
候选瞬间，再逐一验证"换算回本地墙钟是否仍为 W"；无有效候选即缺失时刻，
有多个有效候选取最早者即重复时刻的较早一次。

## 逻辑触发 ID 与去重

逻辑触发 ID = `触发器ID@本地墙钟(分钟)`，如 `weekday-morning@2026-11-01T01:30`。

同一逻辑 ID 只触发一次：重复时刻的两次出现共享同一 ID；时钟回拨导致窗口
重放时，已触发过的逻辑 ID 会被跳过（计入 `dedup_skipped`）。去重表在内存中
保留 48 小时滚动窗口。

## 停机补触发与上限

调度器内部维护 checkpoint，每次推进处理 `[checkpoint, now)`：

- **错过窗口** `[checkpoint, now−1s)`：停机/卡顿期间错过的时刻，每个触发器
  最多补触发 `catch_up_limit` 次（**保留最新的若干次**，最旧的丢弃并计入
  `missed_dropped`）；`catch_up_limit: 0` 表示完全不补触发（默认值 100）。
- **正常窗口** `[now−1s, now)`：本周期按时到期的时刻，始终触发，不受上限影响。
- 触发器**创建之前**的时刻不属于"错过"，不会补触发。
- 补触发产生的事件 `caught_up=true`。

## 固定版本的时区数据库

时区规则不读宿主机 `/usr/share/zoneinfo`，也不用 `time/tzdata` 的隐式回退，
而是把 IANA tz 数据库编译产物 vendor 进仓库并 `go:embed` 进二进制：

- 文件：[`internal/tzdb/zoneinfo.zip`](internal/tzdb/zoneinfo.zip)（取自 Go 1.23.4 发行版）
- 版本：**IANA tzdata 2024a**，由两条规则探针在测试中锁定
  （哈萨克斯坦 2024-03 统一 UTC+5 → ≥2024a；巴拉圭夏令时仍存在 → <2024b）
- SHA-256：`2359012e2cf90a23bf4e9eeaeecc06b7936e9f37b76af97e9dc904b95d6a944a`
- `GET /version` 可在运行时查询上述信息

升级方式：用新版 Go 的 `$GOROOT/lib/time/zoneinfo.zip` 替换该文件，
更新 `internal/tzdb/tzdb.go` 中的 `Version`/`ZipSHA256` 及探针测试。

## HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 健康检查 |
| GET | `/version` | Go 版本、内嵌 tz 数据库版本与 SHA-256 |
| POST | `/triggers` | 创建触发器（201；重复 ID 409；参数非法 400） |
| GET | `/triggers` | 列出全部触发器（含 `next_fire`） |
| GET | `/triggers/{id}` | 单个触发器详情与统计 |
| DELETE | `/triggers/{id}` | 删除（204；不存在 404） |
| GET | `/triggers/{id}/preview?from=&to=&limit=` | 预览窗口内触发时刻（RFC3339，默认未来 7 天） |
| GET | `/events?trigger_id=&limit=` | 查询触发事件（含逻辑 ID、墙钟、是否补触发） |

创建请求体：

```json
{
  "id": "weekday-morning",        // 可选，缺省自动生成 trig-xxxxxxxx
  "minutes": "30",                 // 必填
  "hours": "9",                    // 必填
  "weekdays": "1-5",               // 必填
  "timezone": "America/New_York",  // 必填，IANA 名称
  "catch_up_limit": 10             // 可选，默认 100，0 = 不补触发
}
```

触发事件：

```json
{
  "seq": 61,
  "trigger_id": "every-minute",
  "logical_id": "every-minute@2026-09-24T01:33",
  "wall_clock": "2026-09-24T01:33",
  "timezone": "UTC",
  "fired_at_utc": "2026-09-24T01:33:00Z",
  "caught_up": false
}
```

## 测试

```bash
go test ./...
```

**测试不依赖真实睡眠**：调度核心 `Engine.Advance(now)` 接受显式时刻，
测试用假时钟/显式时间轴驱动，秒级跑完。覆盖：

- 表达式解析（合法/非法/边界）
- 2026 年春季切换：缺失时刻跳过（America/New_York，整小时偏移）
- 2026 年秋季切换：重复时刻只执行较早一次（引擎层 + 表达式层）
- 非整小时 DST（Australia/Lord_Howe，30 分钟偏移的缺失与重复）
- 跨年（周几匹配跨 12 月/1 月边界、每日表达式跨年计数）
- 停机 1 小时补触发上限（60 次错过 → 补 5 次、丢弃 55 次）、上限为 0
- 时钟回拨重放去重（逻辑触发 ID）
- 内嵌 tz 数据库版本探针（锁定 2024a）与 2026 年纽约 DST 切换点
- HTTP 层：生命周期、参数校验、预览、事件查询

## 实测记录（2026-09-24，go1.23.4 linux/amd64）

- `go vet ./...`：通过；`gofmt -l .`：无输出。
- `go test ./...`：**24 个测试全部通过**
  （engine 8、httpapi 4、schedule 8、tzdb 4）。
- 实机运行（`PORT=29731 ./tztrigger`）：
  - `GET /version` 返回 `tzdata_version=2024a` 与预期 SHA-256；
  - 注册 `every-minute`（UTC 每分钟）后约 75 秒，`GET /events` 返回 2 条
    `caught_up=false` 的真实触发事件，逻辑 ID 分别为
    `every-minute@2026-09-24T01:32`、`every-minute@2026-09-24T01:33`；
  - 预览接口验证：秋季窗口 11-01 01:30 仅一条（05:30:00Z，较早一次），
    春季窗口 03-08 无 02:30 触发（缺失跳过）；
  - **停机补触发实机验证**（`kill -STOP` 挂起进程模拟停机，`kill -CONT` 恢复）：
    - 挂起约 2 分 34 秒后恢复：`catchup-demo`（`catch_up_limit=3`）补触发
      01:39/01:40/01:41 共 3 条 `caught_up=true` 事件，随后 01:42 起恢复
      正常按时触发（`caught_up=false`）；
    - 再次挂起 75 秒：`no-catchup`（`catch_up_limit=0`）`total_fired=0`、
      `missed_dropped=1`——完全不补触发但错过次数准确记账；
  - `examples/requests.sh` 已对本机服务完整跑通（含 409 重复 ID、
    400 未知时区等错误分支）。

## 限制与未完成项

- **状态全在内存**：进程重启后触发器、事件日志、checkpoint 全部丢失，
  重启后从"当前时刻"重新开始（不会补触发停机期间——因为连触发器定义也没了）。
  持久化（如 SQLite/JSON 快照）未实现。
- 事件日志为内存环形缓冲，最多保留 10000 条。
- 补触发枚举有保险上限（约逐分钟表达式 2 年的量），极端超长停机+逐分钟
  表达式场景下超出部分不补。
- 去重表保留 48 小时：时钟回拨超过 48h 的重放无法去重（正常部署不会遇到）。
- 无鉴权、无 TLS（假定部署在受信网络/反向代理之后）。
- 触发动作目前只是"记录事件"，未接 webhook/消息队列等真实执行器
  （`Engine` 的事件列表即扩展点）。
