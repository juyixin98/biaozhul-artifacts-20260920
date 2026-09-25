# logmerge — 日志多行归并后端

纯后端 Go 服务（仅标准库，无第三方依赖）：通过 HTTP 摄入原始日志行，
按**起始行规则**把多行日志（如异常堆栈）拼装为完整记录，按**来源独立缓存**，
支持超时与字节上限截断并保留完整性标志，记录持久化到本地 JSONL 文件，
进程重启后不丢失。

## 构建与运行

```bash
go build -o bin/logmerge ./cmd/logmerge
./bin/logmerge -addr 127.0.0.1:8080 -data data/entries.jsonl \
    -max-bytes 65536 -flush-timeout 5s \
    -start-pattern '^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}'
```

参数：

| 参数 | 默认值 | 说明 |
|---|---|---|
| `-addr` | `:8080` | HTTP 监听地址 |
| `-data` | `data/entries.jsonl` | JSONL 持久化文件路径（自动建目录） |
| `-max-bytes` | `65536` | 单条归并记录字节上限，0 为不限 |
| `-flush-timeout` | `5s` | 待归并记录空闲超时，超时按不完整刷出 |
| `-start-pattern` | 时间戳前缀正则 | 起始行判定规则 |

## 归并规则

- 命中 `-start-pattern` 的行为**起始行**：刷出该来源当前待归并记录
  （`complete=true, reason=new_start`），并开启新记录。
- 其余行为**续行**：追加到该来源当前记录；若该来源没有待归并记录，
  则新建一条 `has_start_line=false` 的记录。
- 追加会超过 `-max-bytes` 时：当前记录按 `complete=false, reason=max_bytes`
  截断刷出，该行另起新记录。
- 空闲超过 `-flush-timeout`：按 `complete=false, reason=timeout` 刷出
  （由后台定时器扫描）。
- 进程收到 SIGTERM/SIGINT：所有待归并记录按 `complete=false, reason=shutdown`
  刷出后退出。
- 每个来源（`source` 字段）有独立缓冲区，交错摄入绝不混合。

`complete` 即完整性标志：只有「以起始行开头且因新起始行而收尾」的记录才为 true。

## HTTP API

### `POST /ingest`

请求体为单个对象或对象数组：`{"source": "...", "line": "...", "time": "可选RFC3339"}`。
返回 `202 {"accepted": N}`。

### `GET /logs?source=<可选>&limit=<可选>`

返回已归并并持久化的记录（按写入顺序，`limit` 取最新 N 条）：

```json
{"entries": [{"source": "java-app", "message": "...\n\tat ...", "line_count": 3,
  "byte_size": 160, "complete": true, "reason": "new_start",
  "has_start_line": true, "start_time": "...", "end_time": "..."}], "count": 1}
```

### `GET /healthz`

返回 `200 ok`。

请求样例脚本：[`examples/requests.sh`](examples/requests.sh)（`./examples/requests.sh 127.0.0.1:8080`）。

## 测试

```bash
go test ./...
```

覆盖验收点：

| 验收点 | 测试 |
|---|---|
| 交错两个来源堆栈、无跨来源混合 | `internal/merger` `TestInterleavedSourcesDoNotMix` |
| 无起始行的续行 | `TestLinesWithoutStartLine` |
| 超长异常按字节上限截断并保留完整性标志 | `TestMaxBytesTruncation` |
| 超时截断标志 | `TestTimeoutFlush` / `TestShutdownFlush` |
| 进程重启后数据可恢复 | `internal/store` `TestRestartRecovery` |
| HTTP 摄入/查询/参数校验 | `internal/server` 三个测试 |

## 实际运行记录（2026-09-24，go1.22.2 linux/amd64）

### 自动化测试

```
$ go vet ./... && go test ./...
ok  	logmerge/internal/merger	0.002s
ok  	logmerge/internal/server	0.007s
ok  	logmerge/internal/store 	0.009s
```

10 个测试全部通过，无未通过项。（开发过程中曾有 2 个测试因断言与
设计语义不符失败——shutdown 刷出的记录应为 `complete=false`、字节数
算错导致截断点提前——修正断言后通过；被测实现未改动。）

### 端到端实测（合成数据）

命令：`./bin/logmerge -addr 127.0.0.1:$PORT -data /tmp/lmdemo/entries.jsonl -max-bytes 200 -flush-timeout 2s`

1. **交错摄入** java-app（Java 堆栈 3 行）与 go-worker（Go panic 3 行）
   共 8 行：返回 `{"accepted":8}`；查询得到 2 条 `complete=true,
   reason=new_start` 的记录，各自 3 行，无跨来源混合。✔
2. **超长异常**（起始行 + 12 个栈帧，限 200 字节）：拆为 3 条
   `reason=max_bytes, complete=false` 的记录（各 152 字节）+ 1 条
   50 字节余量记录（超时刷出）。✔
3. **无起始行**的 2 行续行：归并为 `has_start_line=false,
   complete=false` 的一条记录。✔
4. **超时**：空闲记录 2 秒后按 `reason=timeout, complete=false` 刷出。✔
5. **进程重启**：`pkill -TERM` 后日志显示 `shutting down: flushing
   pending records`，`entries.jsonl` 共 10 行；重启进程后
   `GET /logs?source=java-app&limit=2` 正常返回重启前写入的记录。✔

实测备注：本机 18080/18099 端口被其他进程占用，演示改用空闲端口
34217；服务本身绑定与监听正常。

## 目录结构

```
cmd/logmerge/main.go        入口：参数、定时刷出、优雅退出
internal/merger/            多行归并核心（起始行规则、按来源缓存、截断标志）
internal/store/             JSONL 持久化与查询（重启恢复）
internal/server/            HTTP API（/ingest、/logs、/healthz）
examples/requests.sh        请求样例
```
