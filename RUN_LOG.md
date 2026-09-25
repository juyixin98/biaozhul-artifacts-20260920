# 运行记录（Run Log）

本文件如实记录在交付环境中实际执行的命令与结果。环境：Linux
6.8.0-90-generic x86_64，Go 1.22.2，仅用标准库；记录日期 2026-09-24。

## 1. 构建

```
$ go vet ./...        # 无输出（通过）
$ go build ./...      # 通过
```

二进制：

```
$ go build -o bin/server ./cmd/server
$ go build -o bin/synth ./cmd/synth
```

## 2. 自动化测试

```
$ go test -race -count=1 ./...
?   github.com/example/hysteresis-alerter/cmd/server [no test files]
?   github.com/example/hysteresis-alerter/cmd/synth  [no test files]
ok  github.com/example/hysteresis-alerter/internal/engine   1.10s
ok  github.com/example/hysteresis-alerter/internal/httpapi  1.06s
ok  github.com/example/hysteresis-alerter/internal/persist  1.04s
```

共 25 个 `--- PASS`（`go test -v` 计数），`-race` 无告警。覆盖率：

| 包 | 语句覆盖率 |
|---|---|
| internal/engine  | 94.8% |
| internal/httpapi | 90.2% |
| internal/persist | 65.0%（未覆盖部分为 fsync/rename 故障注入等防御分支） |

## 3. 服务实跑

启动（默认 8080 被本机其他进程占用，改用 127.0.0.1:28080）：

```
$ ./bin/server -addr 127.0.0.1:28080 -db ./data/snapshot.json
2026/... no snapshot at ./data/snapshot.json, starting fresh at virtual clock 2026-01-01T00:00:00Z
2026/... hysteresis alerter listening on http://127.0.0.1:28080

$ curl -s http://127.0.0.1:28080/health
{"ok":true,"data":{"clock_now":"2026-01-01T00:00:00Z","status":"ok"}}
```

### 3.1 合成数据四场景（实际输出摘要）

**jitter（阈值附近抖动）**：90/75 交替四次（单次热持续 ≤40s < 60s），全程不触发；
随后连续 90，在虚拟时间 00:04:00（热持续满 60s）产生唯一一次 firing；之后 75（warm
迟滞带 70–80）不恢复；60 起进入恢复倒计时。

```
00:04:00  sample v=90 [firing: pending->firing ... sustained for 1m0s]
```

**gap（长时间缺数）**：00:08:20 firing；无样本 tick 2m30s 跨过 no_data_for=1m 后：

```
00:10:50  mem-gap: firing -> nodata  [nodata]
00:11:00  mem-gap: nodata -> inactive [data_resumed]   # 收到健康样本 50
```

继续静默（再 tick）不产生重复 nodata 通知。

**outoforder（时间倒序）**：

- 乱序未来批次（按 t+80s、t+60s、t+70s 提交）被服务端升序排列，t+70s 时热持续满 20s
  精确 firing 一次；
- 迟到批次（t 之前）两条：一条 `late:true, duplicate:true, overwrote:true`（覆盖更早
  同 ts 样本），一条 `late:true` 新样本；均不参与求值、零事件、时钟不回退。

**band（数值迟滞带）**：firing 后 76/74/78/72 全在 warm 区，告警保持 firing；65 起进入
cold，进入 recovering，到场景结束冷持续 40s（需 60s），未恢复。

### 3.2 最终通知事件流（实际查询结果）

```
$ curl -s http://127.0.0.1:28080/events
00:04:00 cpu-jitter: pending->firing     [firing]
00:08:20 mem-gap:    pending->firing     [firing]
00:10:50 cpu-jitter: recovering->nodata  [nodata]
00:10:50 mem-gap:    firing->nodata      [nodata]
00:11:00 mem-gap:    nodata->inactive    [data_resumed]
00:13:20 qps-ooo:    pending->firing     [firing]
00:14:40 temp-band:  pending->firing     [firing]
```

每个转换点恰好一条通知；持续 hot、持续静默均不重复产生通知。

### 3.3 配置修改重置（实际结果）

```
PUT /rules/qps-ooo  threshold 1000 -> 1300
-> version 1 -> 2, state firing -> inactive, last_value 清空
-> /events?all=true 出现一条 rule_reset（version=2）
-> 默认通知流 /events 不含 rule_reset
```

### 3.4 重启持久化恢复（实际结果）

SIGTERM 优雅退出：`snapshot saved to ./data/snapshot.json`。重新启动：

```
restored state from ./data/snapshot.json: clock_now=2026-01-01T00:17:00Z rules=4 events=7
cpu-jitter: nodata ver=1
mem-gap:   inactive ver=1
qps-ooo:   firing ver=1
temp-band: recovering ver=1
```

时钟、规则版本、运行时状态、事件序列均从磁盘恢复；恢复后继续 tick，状态机正常求值
（temp-band 在冷持续满 60s 时产生 resolved）。

### 3.5 curl 走查脚本

```
$ bash examples/requests.sh     # exit=0
```

12 个步骤全部成功；事件类型计数（`/events?all=true`）：
`{data_resumed:1, firing:1, nodata:1, resolved:1}`，外加 1 条 rule_reset 审计事件。

## 4. 开发过程中遇到并修正的问题（如实记录）

1. **事件 From 字段初版取值错误**：先改状态再读 `from`，导致 From==To；已改为转换前快照。
2. **`trigger_for=0` 语义**：最初零时长被默认值覆盖为 1m；改为“0 表示立即触发/恢复”，
   仅 `no_data_for` 在缺省时取默认 2m，并补测试。
3. **重复样本判定**：独立解析的等价时间戳存在亚纳秒差异导致 map key 不匹配；统一对样本
   时间戳按秒截断（UTC）后修复。
4. **warm 在 recovering 中的语义**：warm 应“挂起恢复倒计时并留在 recovering”，只有真正
   hot 才无通知回到 firing；测试预期与实现已对齐。
5. 演示脚本 `examples/requests.sh` 初版时序算错（热持续不足 60s 却期待 firing）且 jq
   不容忍被省略的空 `events` 字段；已修正时序并用 `(.events // [])` 容错。

## 5. 未通过项 / 已知限制

- 无未通过的测试或验收项。
- 已知限制（样例性质，非缺陷）：
  - 持久化为单机单 JSON 文件，不支持多副本并发；
  - 每 metric 仅保留最新 1000 条样本、最新 2000 条事件（引擎常量可调）；
  - 样本时间戳精度为秒；不提供鉴权/TLS；无前端（按要求）。
