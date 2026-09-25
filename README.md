# 制品差量更新（Artifact Delta Update）— 纯后端

一个纯 Go 标准库实现的**本地**构建工程服务：对制品（artifact）做
**块级差量补丁**的生成与**原子应用**。不依赖任何第三方库，不连接任何云平台；
内容寻址**缓存目录**与目标**工作目录**严格分离。

- 语言：Go 1.22（仅标准库）
- 形态：本地 HTTP JSON 服务 + 命令行工具（同一二进制）
- 前端：无（按要求不做前端）

## 1. 它保证什么

1. **补丁绑定旧、新两份摘要**
   - 应用前，磁盘上的现有制品必须哈希等于补丁 `oldSum`，否则以
     `wrong_base` 拒绝（错基线）。
   - 替换前，重建结果必须哈希等于补丁 `newSum`，否则丢弃临时文件、
     保留旧制品。
   - 补丁自身还有 `patchSum`（对除该字段外的规范化补丁 JSON 取
     SHA-256），任何对补丁字节的截断、翻转都会在**写出任何产物之前**
     以 `corrupt_patch` 拒绝。

2. **原子应用，旧制品始终可用**
   - 新内容写入同目录临时文件 `目标.delta.tmp`，校验通过后用**一次
     `rename(2)`** 切换目录项。切换之前，读取方通过原路径看到的永远是
     旧制品；进程从不原地覆盖正在读取的文件。
   - 切换之后若再失败（断电、崩溃），目标已是新制品；重跑应用会识别
     “已在 newSum”并幂等返回，不产生半成品。
   - 切换之前任一步失败（含硬崩溃）：临时文件由延迟清理或下一次应用在
     锁内清除；旧制品字节与路径名都不变。
   - 每个目标一把 `flock(2)` 咨询锁，串行化并发应用；硬崩溃时内核自动
     释放，不会死锁。

3. **空间不足不动旧制品**
   - 写临时文件之前对目标目录做 `statfs` 可用空间预检
     （需要 `newSize + slack`），不足直接返回 `insufficient_space`，
     不创建/不改动任何目标内容。

4. **插入导致的块位移不影响块复用**
   - 采用 rsync 风格的**内容定义匹配**：旧块的弱滚动校验
     （16 位模 Adler 派生和）建索引 + SHA-256 强校验确认；在新制品上
     滑动窗口。即使在文件开头插入若干字节、所有固定边界都发生位移，
     窗口滚到与旧块对齐的位置仍会命中。本仓库测试中 40 个块、偏移
     37 字节插入的样例可复用 39 个块。

5. **缓存 / 工作目录分离**
   - 缓存：`--cache DIR`，内容寻址，仅以哈希暴露，上传只落缓存。
   - 工作目录：`--work DIR`，应用目标只在其下，路径穿越（`..`）被拒。
   - 服务启动时若两个目录相同会直接报错。

## 2. 目录结构

```
go.mod
cmd/delta-update/            CLI + 本地 HTTP 服务（main 包）
  main.go  serve.go  artifact.go  delta_cmd.go  apply_cmd.go  inspect.go  util.go
internal/
  store/                     内容寻址缓存（temp+rename 原子落盘）
  delta/                     补丁结构、弱滚动哈希、生成（Generate）
    rolling.go               rsync 式滚动校验和
    patch.go                 Patch/Op、绑定摘要、严格 JSON、校验
    generate.go              签名表 + 新制品滑动扫描
  apply/                     原子应用
    apply.go                 主流程与各阶段
    reconstruct.go           copy/data 操作重建（只读旧制品 Seek）
    stages.go                阶段常量、故障注入策略、空间检查接口
    space_linux.go           statfs 空间预检
    fs.go lock_linux.go      目录 fsync、flock 锁（含非 Linux 兜底）
  server/                    JSON HTTP API
test/crash/                  子进程硬崩溃验收夹具（编译真实 CLI 再打崩）
examples/                    请求样例文档与端到端脚本
```

## 3. 构建

```bash
go build ./...
go vet ./...
go test ./...
```

仅用标准库，`GOPROXY=off` 下也可构建与测试（CI 无网络可用）。

## 4. 运行本地服务

```bash
go run ./cmd/delta-update serve \
  --addr 127.0.0.1:8080 \
  --cache ./data/cache \
  --work  ./data/work
# 可选：--max-upload-bytes 536870912
```

默认监听回环地址；没有任何出站连接。

### HTTP 接口一览（详见 `examples/requests.md`）

| 方法与路径 | 说明 |
|---|---|
| `GET  /v1/health` | 健康检查 |
| `POST /v1/artifacts` | 上传制品原始字节到缓存，返回 sha256 |
| `GET  /v1/artifacts/{hash}` | 从缓存取制品 |
| `POST /v1/deltas` | 入参 `{oldRef,newRef,blockSize}`，返回绑定摘要的补丁 |
| `POST /v1/apply` | 入参 `{target,patch}`，原子应用；支持夹具字段 `failStage`、`simFreeBytes` |
| `GET  /v1/work/{path...}` | 查工作目录目标的大小与摘要 |
| `PUT  /v1/work/{path...}` | 预置/覆盖工作目录目标（演示/夹具，原子写） |

`oldRef/newRef` 既可以是缓存中的 64 位哈希，也可以是**绝对路径**。
`target` 是相对工作目录的路径，服务端会规范化并拒绝 `..` 越界。

错误体统一为 `{"error":{"code":..., "message":...}}`，关键 code：
`wrong_base`(409)、`corrupt_patch`(422)、`insufficient_space`(507)、
`locked`(409)、`bad_target`(400)。

## 5. 命令行

```bash
# 上传本地文件到缓存，打印哈希
delta-update artifact put --cache ./data/cache ./old.bin

# 生成补丁（ref 可为缓存哈希或绝对路径），写到文件或 stdout
delta-update delta --cache ./data/cache \
  --old ./old.bin --new ./new.bin \
  --block-size 1024 --patch ./patch.json

# 原子应用
delta-update apply --target ./data/work/app/current.bin --patch ./patch.json

# 查看摘要
delta-update inspect ./data/work/app/current.bin
```

`apply` 的退出码：0 成功；10 错基线；11 补丁损坏；12 空间不足；
13 目标被锁；14 注入的可恢复故障；42 模拟硬崩溃（见下）。

### 故障/崩溃注入（夹具专用，仅 CLI 环境变量）

硬崩溃通过 `os.Exit(42)` 真实终止进程，**刻意**留下临时文件与锁
（锁由内核释放），用来验收崩溃恢复：

| 环境变量 | 含义 |
|---|---|
| `DELTA_CRASH_STAGE=<stage>` | 在该阶段硬退出 42 |
| `DELTA_FAULT_STAGE=<stage>` | 在该阶段返回可恢复错误（退出码 14） |
| `DELTA_FAULT_AFTER_BYTES=<n>` | write-delta 阶段写出 n 字节后触发 |
| `DELTA_SIM_FREE_BYTES=<n>` | 让空间预检看到 n 字节可用 |

阶段（与代码中的顺序一致）：
`check-old → space → prepare → write-delta → sync → verify-delta →
rename → post-rename → fsync-dir → verify-new`。

示例：

```bash
DELTA_CRASH_STAGE=write-delta DELTA_FAULT_AFTER_BYTES=4096 \
  delta-update apply --target work/x --patch p.json   # 退出 42，旧制品完好
delta-update apply --target work/x --patch p.json     # 重跑，收敛到 newSum
```

## 6. 补丁格式（`delta-patch/v1`）

```json
{
  "version": "delta-patch/v1",
  "blockSize": 1024,
  "oldSize": 73728,
  "newSize": 73759,
  "oldSum": "<sha256 hex>",
  "newSum": "<sha256 hex>",
  "ops": [
    {"type": "copy", "oldIndex": 3},
    {"type": "data", "data": "<标准 base64 字面字节>"}
  ],
  "patchSum": "<sha256 hex of canonical JSON excluding patchSum>"
}
```

- `copy`：从旧制品第 `oldIndex` 块取 `blockSize` 字节（最后一块可为短块）。
  块引用**允许任意顺序与重复**（重建按偏移 `Seek`，而不是顺序读旧文件），
  因此“新制品重复旧内容”也能高复用。
- `data`：内联字面字节（JSON 中为标准 base64）。
- 解析使用 `DisallowUnknownFields`，并由 `Validate` 重算操作总长度，
  与 `newSize` 不一致即非法。

## 7. 原子应用的阶段与崩溃语义

```
取目标 flock
 └─ check-old      现有目标摘要：==newSum → 幂等返回；!=oldSum → wrong_base
 └─ space          statfs 预检 newSize+slack；不足 → insufficient_space
 └─ prepare        清除上次崩溃残留的 .delta.tmp；O_EXCL 建新临时文件
 └─ write-delta    copy(Seek 旧块,只读) / data 流式写入临时文件
 └─ sync           fsync(temp)
 └─ verify-delta   临时文件摘要必须 == newSum（同时验证补丁绑定）
 └─ rename         一次 rename：临时文件 → 目标路径   ← 唯一可见切换点
 └─ post-rename    （仅崩溃夹具落点）
 └─ fsync-dir      fsync 父目录，持久化目录项
 └─ verify-new     用最终路径重新打开读回，摘要必须 == newSum
释放锁
```

- rename 之前任何返回/崩溃：延迟清理或下次 prepare 清掉临时文件；
  旧制品路径与字节不变，仍可被读取。
- rename 之后任何返回/崩溃：目标已是新内容；重跑命中幂等分支。
- 全程旧制品只被 `Open`（只读）与 `Seek/Read`，没有任何写句柄。

## 8. 自动化测试与验收映射

```bash
go test ./...                 # 全部
go test ./test/crash -v       # 子进程硬崩溃夹具（会先 go build 真实 CLI）
```

| 验收点 | 测试 |
|---|---|
| 插入导致块位移仍高复用 | `internal/delta`：`TestRoundTripInsertionShiftsBlocks`（40 块复用 39）、`TestRoundTripRandomFuzz`（60 组随机插入/删除/替换） |
| 滚动校验和正确性 | `TestRollerMatchesNaive`（与朴素实现逐窗口比对） |
| 错基线拒绝且不改动文件 | `apply`：`TestApplyWrongBase`；`server`：`TestApplyWrongBaseOverHTTP`；CLI：`TestCLIWrongBase`(退出码 10) |
| 损坏补丁拒绝且不改动文件 | `TestApplyCorruptPatch`、`TestApplyCorruptPatchOverHTTP`、`TestCLICorruptPatch`(11) |
| 空间不足预检 | `TestApplyNoSpace`、`TestSimulatedSpaceShortageOverHTTP`、`TestCLISpacePreflight`(12) |
| 应用各阶段崩溃 | `fault_test.go`（9 个可恢复阶段逐一注入 + 重试收敛）、`test/crash`（10 个阶段真实 `os.Exit(42)` 后旧制品可用 + 重跑核验 newSum） |
| 连续两次写一半崩溃（残留临时清理） | `TestCrashThenCrashAgain` |
| 不原地改正在读取的文件 | `TestApplyOldReadHandleStable`（旧 fd 始终读到旧字节、inode 经 rename 改变） |
| rename 后幂等 | `TestApplyIdempotentAfterRename`、HTTP 全流程中的 reapply |
| 缓存/工作目录分离 | `TestCacheWorkSeparation`、`TestSameDirRejected`、`TestPathTraversalRejected` |
| 端到端 HTTP | `examples/requests.sh`（见“实际运行记录”） |

## 9. 安全边界与刻意不做的事

- 只监听回环、无出站连接、无云平台、无 shell 执行、无第三方依赖。
- 补丁 JSON 严格解析（拒绝未知字段）；字面操作、块索引、总长度均有上限
  与一致性校验。
- 目标路径限定在工作目录内；缓存对象仅以哈希索引，哈希非法即拒绝。
- HTTP 不提供硬崩溃注入（避免远端可触发进程退出）；硬崩溃只在本机 CLI
  通过环境变量用于夹具。
- 不做前端；不做多目标目录的垃圾回收（未列入本次需求）。
