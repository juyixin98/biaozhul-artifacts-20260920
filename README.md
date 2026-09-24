# tztrig — 带时区的定时触发器服务

纯后端、无界面的定时表达式服务。仅用 Go **标准库 `net/http`**，零第三方依赖。
支持 **分钟 / 小时 / 周几** 三段表达式与 **IANA 时区**，并正确处理夏令时：

- **春令时缺失时刻（gap）跳过**：被时钟跳过的挂钟分钟（如纽约 2024-03-10 `02:30`）不触发。
- **秋令时重复时刻（overlap）只执行较早一次**：同一挂钟分钟出现两次（EDT 与 EST），只在第一次（UTC 较早）触发。
- **按逻辑触发 ID 去重**：触发 ID = `scheduleId:事件UTC秒`，同一逻辑触发绝不执行两次。
- **长时间停机补触发有上限**：重启后每个计划最多补发最近 N 次，其余丢弃并记录跳过区间，水位线前移防止积压。

## 依赖与版本锁定

| 项目 | 版本 | 锁定方式 |
|---|---|---|
| Go | 1.23（在 1.23.4 上构建/测试） | `go.mod` 中 `go 1.23`，构建用 `GOTOOLCHAIN=local` 禁止工具链自动下载漂移 |
| Go 第三方模块 | **无** | `go.mod` 无 require，因此无需 `go.sum`；标准库随工具链固定 |
| IANA tzdata | **2024a**（固定） | 不使用宿主 `/usr/share/zoneinfo`；由 `scripts/build-zoneinfo.sh` 下载、**校验 sha256**、`zic` 编译后通过 `go:embed` 内嵌进二进制 |

> 关键：进程加载的时区数据**只有内嵌的 2024a**。因此在任何宿主（其 tzdata 可能是 2026c 等其它版本）上，
> 春秋切换行为完全一致、可复现。这也是验收测试不依赖宿主时区库的原因。

tzdata tarball 校验和（`scripts/build-zoneinfo.sh` 内固化）：

```
0d0434459acbd2059a7a8da1f3304a84a86591f6ed69c6248fffa502b6edffe3  tzdata2024a.tar.gz
```

重建内嵌时区库需要 `zic`（`tzdata`/系统自带）与网络；正常构建**不需要**（编译产物已提交在 `internal/zoneinfo/files/`）。

## 目录结构

```
cmd/tztrig/main.go            启动入口：HTTP 服务 + 调度循环 + 优雅停机 + 启动补触发
internal/schedule/            表达式解析 + 未来触发时刻推算（含 DST gap/overlap 语义）
internal/engine/              时间无关的核心：状态机 Advance、去重、补触发上限、JSON 快照
internal/zoneinfo/            仅从内嵌 tzdata2024a 加载时区
internal/api/                 net/http 路由与 JSON 接口
internal/zoneinfo/files/      zic 编译并内嵌的固定时区库（597 个文件，含 backward 别名）
scripts/build.sh              可复现构建（GOTOOLCHAIN=local, CGO_ENABLED=0, -trimpath）
scripts/build-zoneinfo.sh     下载→校验→zic 编译内嵌时区库
examples.sh                   全部 HTTP 接口的 curl 请求样例
Dockerfile                    多阶段静态构建（运行镜像无需时区包/网络）
```

## 启动命令

需要 Go 1.23+。

```bash
# 构建（可复现，无网络依赖，除已提交的内嵌时区库外）
make build            # 或 ./scripts/build.sh

# 运行
./bin/tztrig -addr=:8080 -state=data/state.json -catchup=100
```

命令行参数：

| 参数 | 默认值 | 说明 |
|---|---|---|
| `-addr` | `:8080` | HTTP 监听地址 |
| `-state` | `data/state.json` | JSON 快照路径（原子写：临时文件 + rename）。传空字符串禁用持久化 |
| `-catchup` | `100` | 停机后每个计划最多补发的触发数 |

Docker：

```bash
docker build -t tztrig .
docker run --rm -p 8080:8080 tztrig
```

## 表达式格式

三段，顺序 `minute hour weekday`，空串或 `*` 表示“每”：

- **minute**：`0-59`
- **hour**：`0-23`
- **weekday**：`0-6`，周日 = 0（也接受 7 = 周日）

每段支持：单值 `n`、区间 `a-b`、列表 `a,b,c`、步长 `*/k`、`a-b/k`、`n/k`（从 n 到上界按 k）。

示例：

| 含义 | minute | hour | weekday |
|---|---|---|---|
| 每分钟 | `*` | `*` | `*` |
| 每小时整点 | `0` | `*` | `*` |
| 每天 02:30（本地） | `30` | `2` | `*` |
| 工作日 09:00 | `0` | `9` | `1-5` |
| 周末每 15 分钟 | `*/15` | `*` | `0,6` |

时区用 IANA 名，如 `America/New_York`、`Europe/Berlin`、`Asia/Shanghai`、`Australia/Sydney`、`UTC`，
也接受 backward 别名（如 `US/Eastern`）。可用 `GET /api/zones` 列举。

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 健康检查 |
| GET | `/api/info` | tzdata 版本、补触发上限、计划数 |
| GET | `/api/zones?prefix=` | 列举内嵌时区，可按前缀过滤 |
| POST | `/api/schedules` | 创建计划 |
| GET | `/api/schedules` | 列出全部计划 |
| GET | `/api/schedules/{id}` | 查询单个计划（含 `lastFired` 水位线） |
| DELETE | `/api/schedules/{id}` | 删除 |
| POST | `/api/schedules/{id}/pause` | 暂停（暂停期间不推进水位线） |
| POST | `/api/schedules/{id}/resume` | 恢复（恢复后按上限补发暂停期间的触发） |
| GET | `/api/schedules/{id}/next?n=3` | 预览未来 n 次触发（n 1-100，默认 3） |
| GET | `/api/fires?limit=50` | 最近已投递触发（1-500，默认 50） |
| GET | `/api/skips` | 最近一次补触发被上限丢弃的区间 |

创建请求体：

```json
{
  "id": "nightly-ny",
  "minute": "30",
  "hour": "2",
  "weekday": "*",
  "timezone": "America/New_York",
  "enabled": true
}
```

触发记录字段：

```json
{
  "id": "nightly-ny:1710110400",   // 逻辑触发ID = scheduleId:事件UTC秒
  "scheduleId": "nightly-ny",
  "eventTime": "2024-03-11T06:30:00Z",
  "reason": "due",                 // due=按时；catchup=停机补发
  "firedAt": "2024-03-11T06:30:01Z"
}
```

完整可运行样例见 [`examples.sh`](./examples.sh)。

## DST 语义（实现要点）

Go 的 `time.Date` 对不存在/歧义的挂钟时刻会自行归一化，行为不适合直接表达调度语义。
本项目在 `internal/schedule` 中用 `resolveWall` **显式枚举**挂钟分钟的所有可能映射：

1. 取候选时刻及其前后 24h 处的 UTC 偏移（一天内至多两次偏移，足以覆盖该挂钟分钟的全部映射）；
2. 对每个候选偏移，反算“挂钟字段 + 该偏移”对应的 UTC 瞬间，并校验其在本时区的挂钟字段确实吻合；
3. **无任何吻合 → gap，跳过该分钟**；多个吻合（overlap）→ **取 UTC 最早的一个**。

因此：
- 纽约 2024-03-10 的 `02:00–02:59` 全部不存在，每分钟/每小时调度在该日该时段零触发；
- 纽约 2024-11-03 的 `01:30` 有 `05:30Z`(EDT) 与 `06:30Z`(EST) 两个瞬间，只在 `05:30Z` 触发一次。

## 测试（禁止真实睡眠）

全部测试通过 **注入式时钟**（`engine.Clock` 接口 + `engine.FakeClock`）和 `httptest` 驱动，
没有任何 `time.Sleep`，毫秒级完成、确定性可重复。

```bash
make test     # go test -count=1 ./...
make race     # 竞态检测
make cover    # 覆盖率
```

覆盖的验收场景：

- **春切 gap 跳过**：纽约 2024-03-10（北半球）、悉尼 2024-10（南半球）；
- **秋切 overlap 只执行较早一次**：纽约 2024-11-03、悉尼 2024-04-07；
- **跨年**：2024→2025 的周一/每日/整点补发；
- **长时间停机补触发上限**：FakeClock 一次跳跃模拟 30 天 / 24 小时停机，断言只补最近 N 次、
  其余计入 `skipped`、水位线前移、再次 Advance 不回放；
- **逻辑 ID 去重**：重复 Advance 同一时刻不重复触发，ID 全局唯一；
- 暂停/恢复、表达式与时区校验、快照持久化往返、HTTP 全生命周期、404/400/405 状态码。

所有时刻断言基于**固定 tzdata 2024a** 的已知转换（已用 `zdump` 交叉核对）。

## 持久化与停机补发

- 每次创建/删除/启停/触发后原子写 JSON 快照（计划、水位线 `lastFired`、已投递 ID 表）。
- 启动时加载快照并立即 `Advance(now)`：枚举每个启用计划在 `(lastFired, now]` 内的全部到期时刻，
  超过 `-catchup` 的部分仅补发最近 N 次，更早的丢弃并在内存中通过 `GET /api/skips` 暴露丢弃区间，
  水位线无条件前移到 `now`，杜绝下次重启重复枚举庞大积压。
- `lastFired` 只在启用且处理时推进；暂停期间不补发，恢复后按上限补发。

## 设计边界（未做 / 已知约束）

- 只有 minute/hour/weekday 三段，**不含“月 / 日 / 秒”**（按需求规格）。
- 触发动作目前只记录到内存历史 + 日志 + 快照；**没有 webhook/外发执行器**（需求只要求触发器与接口）。
- 单实例、文件快照持久化；未做分布式锁/多副本（单文件状态不适合多实例并发写）。
- 调度循环每秒级前瞻唤醒，进程刚启动那一瞬间到期的触发由启动 `Advance` 兜住；不保证亚秒级精度。
- 历史在内存保留最近 500 条（快照不存历史，只存去重 ID 表，该表保留近 30 天）。
