# cgroup-analyzer

cgroup v2 离线采样分析服务。从项目内 fixture 目录读取容器的 CPU、内存与压力（PSI）指标，
对累计计数器做差分得到速率，并基于证据区分 OOM 事件、达到内存上限与普通进程退出。
纯后端 HTTP API，技术栈：Go + Chi + SQLite（`modernc.org/sqlite`，纯 Go 无 cgo）。

## 本地启动

```bash
go build -o bin/cgroup-analyzer ./cmd/server
./bin/cgroup-analyzer -addr :8080 -fixtures fixtures -db :memory:
```

参数：

| 参数 | 默认值 | 说明 |
|---|---|---|
| `-addr` | `:8080` | 监听地址 |
| `-db` | `:memory:` | SQLite DSN，如 `file:data.db` 持久化 |
| `-fixtures` | `fixtures` | fixture 根目录（须含 `containers/`） |

启动时自动 ingest；运行中可用 `POST /api/ingest` 重新读取 fixture 目录并整体替换库内数据。

## 验收命令

```bash
go vet ./...
go test ./...                # 全部单元测试 + 基于 fixture 的端到端 API 测试

# 手工验收（服务已启动）：
curl -s localhost:8080/api/containers
curl -s localhost:8080/api/containers/web/instances/boot-1/report
curl -s localhost:8080/api/containers/batch/instances/run-1/rates
curl -s localhost:8080/api/containers/batch/instances/run-1/events
curl -s -X POST localhost:8080/api/ingest
```

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 健康检查 |
| POST | `/api/ingest` | 重新加载 fixture 目录，原子替换数据库 |
| GET | `/api/containers` | 列出所有 容器/实例 及样本数、时间范围 |
| GET | `/api/containers/{c}/instances/{i}/report` | 完整分析报告（区间速率 + 事件） |
| GET | `/api/containers/{c}/instances/{i}/rates` | 仅区间速率 |
| GET | `/api/containers/{c}/instances/{i}/events` | 仅事件（含证据） |

## Fixture 目录格式（显式限定）

```
fixtures/containers/<container>/<instance>/samples/<NNNNNN>/
    timestamp         单个整数，Unix 秒（必需；缺失或非法则该样本加载失败）
    cpu.stat          cgroup v2 格式，每行 "<key> <uint64>"，已知键：
                      usage_usec user_usec system_usec nr_periods nr_throttled throttled_usec
    memory.current    单个无符号整数，字节
    memory.max        单个无符号整数（字节）或字面量 "max"（无限制）
    memory.events     每行 "<key> <uint64>"，已知键：
                      low high max oom oom_kill oom_group_kill
    cpu.pressure      PSI 两行：some/full avg10=<f> avg60=<f> avg300=<f> total=<uint64>
    memory.pressure   同上
fixtures/containers/<container>/<instance>/exit.json   （可选）
    {"code": 0, "reason": "completed", "time": 1700000060}
```

规则：

- 已知文件中的未知键：忽略并记录 warning；格式错误的行：加载失败并报告文件与行号。
- 样本内单个可选文件缺失：不中断加载，对应字段为 `null`，样本记录 warning
  （fixture `batch/run-1` 的 `000004` 即缺 `memory.current`）。
- `<container>` 可对应多个 `<instance>` 目录（同名不同实例，如 `web/boot-1` 与 `web/boot-2`），
  各自独立分析，计数器互不影响。
- 样本目录号为连续序号；跳号表示采样丢失（如 `batch/run-1` 缺 `000003`）。

## 计算口径（单位与采样间隔）

- **采样间隔**：取自每个样本自带的 `timestamp`，不做等间隔假设；`dt = t[i+1] - t[i]` 秒。
  `dt <= 0` 时该区间不产生速率并记录 warning。
- **CPU 速率**：`cores = Δusage_usec / (dt × 1e6)`，1.0 = 占满一个核。
- **节流比率**：`Δthrottled_usec / (dt × 1e6)`，与 cores 同量纲。
- **PSI 停滞速率**：`Δtotal / dt`，单位 微秒停滞/秒（×100 即百分比）。
- **内存**：`memory.current`/`memory.max` 为采样瞬时值（字节），非计数器，不做差分；
  使用率 = current/max。
- **计数回退 / 实例重建**：任一累计计数器出现 `cur < prev`，该区间速率输出 `null`
  （**绝不产生负速率**），并记录 `counter_reset` 事件及证据。

## 事件判定（返回证据而非猜测）

| 事件类型 | 判定依据（证据） |
|---|---|
| `oom_kill` | `memory.events` 的 `oom_kill` 计数在区间内的增量 > 0；证据含前后计数值与样本号 |
| `memory_max_hit` | `memory.events` 的 `max` 计数增量 > 0（分配触及 `memory.max`） |
| `counter_reset` | 累计计数器回退的前后数值 |
| `sample_gap` | 样本序号跳变，列出缺失序号 |
| `normal_exit` | 存在 `exit.json` 且 `code=0`，且全程 `oom_kill` 未增长 |
| `abnormal_exit` | `exit.json` 非零退出码，且 `oom_kill` 未增长（明确不是 OOM） |
| `incomplete_data` | 样本中断且无 `exit.json`：如实报告无法判定，不猜测 |

## 手算样例（与 API 输出一致，见测试）

`batch/run-1`（4 个样本，序号 1,2,4,5，时间 100/110/130/140 秒）：

- 区间 1→2：`Δusage=3,000,000-1,000,000=2,000,000 µs`，`2e6/(10×1e6)=0.2` 核。
- 区间 2→4：缺样本 3（`sample_gap`，dt=20s）；`usage_usec` 由 3,000,000 回退到 500
  → `counter_reset`，`cpu_cores=null`。
- 区间 4→5：`Δusage=1,000,500-500=1,000,000 µs` → `0.1` 核；
  `memory.current == memory.max` → 使用率 `1.0`；`max` 计数 0→2 → `memory_max_hit`。
- 结尾：`exit.json code=0` 且 `oom_kill` 全程为 0 → `normal_exit`。

`web/boot-1`：6 样本等间隔 10s，每区间 `Δusage=5,000,000 µs` → 恒 `0.5` 核；
seq 5 处 `oom_kill` 0→1 → `oom_kill`；`exit.json code=137` + oom 证据 → 结局判定为 OOM。

对应断言见 `internal/analysis/analysis_test.go` 与 `internal/api/server_test.go`。

## 项目结构

```
cmd/server/main.go          入口：加载 fixture → SQLite → HTTP 服务
internal/cgroup/            cgroup v2 文件解析（严格格式校验）
internal/fixture/           fixture 目录加载（缺文件告警、坏文件报错）
internal/analysis/          差分速率、计数重置保护、事件判定
internal/store/             SQLite 存储（原始样本入库，读取时计算）
internal/api/               Chi 路由与 JSON 响应
fixtures/                   示例输入（web/boot-1, web/boot-2, batch/run-1）
```

依赖已锁定（`go.mod` / `go.sum`）：`github.com/go-chi/chi/v5 v5.1.0`，
`modernc.org/sqlite v1.29.10`。
