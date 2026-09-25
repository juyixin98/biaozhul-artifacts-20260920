# patchsvc — 补丁上下文应用服务（纯后端）

一个本地构建工程服务：通过 JSON/HTTP 接口接收**统一 diff（unified diff）子集**，
对本地工作目录做**精确、可校验、可整体回滚**的补丁应用，并可按调用方显式给出的
命令运行测试夹具。

设计约束：

- **纯后端**：无任何前端；仅暴露 JSON 接口。
- **不连接云平台**：服务只监听本地地址，不发起任何出站网络请求。
- **缓存与工作目录分离**：所有暂存/备份状态写入启动时指定的 `-cache-dir`，
  且每个请求都会校验缓存目录与工作目录互不包含，否则拒绝服务。
- **仅运行用户显式提供的测试夹具命令**：`/v1/test` 直接把请求体中的 argv
  交给 `exec.Command` 执行——不经过 shell、不自行推断或补充任何命令。
- **不做模糊猜测匹配**：行号、上下文、新增/删除行必须逐字节精确一致；
  绝不“就近找一个差不多的位置”应用。

## 目录结构

```
cmd/patchsvc/main.go        服务入口（-addr, -cache-dir）
internal/diff/              统一 diff 子集解析器（严格校验）
internal/apply/             精确应用器：Plan（纯校验）→ Publish（备份+回滚）
internal/server/            JSON/HTTP 接口层
examples/                   请求样例（可直接 curl --data @file）
```

## 构建与运行

```sh
go build -o bin/patchsvc ./cmd/patchsvc
./bin/patchsvc -addr 127.0.0.1:8787 -cache-dir /path/to/cache
```

`-cache-dir` 缺省为 `os.UserCacheDir()/patchsvc`。缓存目录不得位于任何
工作目录之内，工作目录也不得位于缓存目录之内。

## JSON 接口

### `GET /v1/healthz`

```json
{"ok": true, "cache_dir": "/path/to/cache"}
```

### `POST /v1/apply` — 校验并原子应用补丁批次

请求（`patch` 为单个补丁文本，`patches` 为批次，可同时使用）：

```json
{
  "workdir": "/abs/path/to/workdir",
  "patch": "--- a/f.txt\n+++ b/f.txt\n@@ -1 +1 @@\n-old\n+new\n",
  "patches": ["--- /dev/null\n+++ b/added.txt\n@@ -0,0 +1 @@\n+hi\n"]
}
```

成功（HTTP 200）：

```json
{"ok": true, "changed": [{"path": "f.txt", "action": "modify"},
                          {"path": "added.txt", "action": "create"}]}
```

校验失败（HTTP 422，**工作目录逐字节不变**）：

```json
{"ok": false, "error": "nonl.txt: hunk 1 context mismatch at old line 1: file has \"no newline file\", patch expects \"WRONG CONTEXT\""}
```

行为保证：

1. **先验证，后发布**：整个批次（所有补丁、所有文件、所有 hunk）先在内存中
   完成解析与精确校验；任一失败则不触碰磁盘。
2. **整体发布**：发布前把每个受影响的现有文件快照到缓存目录；写入经
   “临时文件 + rename” 完成；任一步失败即按快照回滚，目录保持逐字节不变。
3. 同一批次中同一路径出现两次会被拒绝（避免顺序依赖的隐式语义）。

### `POST /v1/test` — 运行调用方显式给出的测试夹具命令

```json
{
  "workdir": "/abs/path/to/workdir",
  "command": ["go", "test", "./..."],
  "timeout_seconds": 60
}
```

响应（HTTP 200；`ok` 表示退出码为 0）：

```json
{"ok": true, "exit_code": 0, "stdout": "...", "stderr": "",
 "duration_ms": 123, "timed_out": false}
```

`command` 为 argv 数组，直接执行，**不经过 shell**；超时上限 10 分钟，
stdout/stderr 各截断到 1 MiB。

## 支持的统一 diff 子集与校验规则

解析器（`internal/diff`）接受：

- `diff --git` / `index` / `new file mode` / `deleted file mode` 等元数据行（忽略）；
- `--- a/路径` / `+++ b/路径` 文件头（Tab 后的时间戳会被剥离）；
  `--- /dev/null` 表示新建文件，`+++ /dev/null` 表示删除文件；
- `@@ -旧起点,旧行数 +新起点,新行数 @@` hunk 头（行数可省略，缺省为 1）；
- hunk 体：` ` 上下文、`-` 删除、`+` 新增、`\ No newline at end of file` 标记。

应用器（`internal/apply`）强制：

- **路径校验**：拒绝绝对路径、`..`、`.`、空分量、反斜杠、NUL；
  拒绝穿越符号链接写入；解析结果必须落在工作目录内。
- **行号校验**：hunk 声明的旧文件起点/行数必须落在文件范围内；
  头部声明行数与 hunk 体实际行数必须一致。
- **上下文校验**：上下文行与删除行必须在声明位置与文件内容逐字节相等，
  否则报 `ContextMismatchError`（带文件、hunk 序号、行号、双方内容）。
- **重叠 hunk 校验**：同一文件的 hunk 在旧文件坐标系中不得重叠
  （相邻允许，交错拒绝）。
- **末尾换行校验**：hunk 触及文件末尾时，`\ No newline at end of file`
  标记必须与文件实际 EOL 状态一致；结果的 EOL 状态由新侧标记决定。
- **文件增删**：新建要求目标不存在；删除要求 hunk 覆盖文件全部行；
  删除/新建同样走上述全部校验。

## 请求样例

`examples/` 下为可直接使用的请求体（把 `workdir` 改成你的绝对路径）：

```sh
# 成功批次：修改 + 新建 + 删除 + 无末尾换行修改，共 4 个文件补丁
curl -s -X POST http://127.0.0.1:8787/v1/apply \
  -H 'Content-Type: application/json' --data @examples/apply-request.json

# 失败批次：第二个补丁上下文错误 → HTTP 422，工作目录逐字节不变
curl -s -X POST http://127.0.0.1:8787/v1/apply \
  -H 'Content-Type: application/json' --data @examples/apply-bad-request.json

# 运行显式给出的测试夹具命令
curl -s -X POST http://127.0.0.1:8787/v1/test \
  -H 'Content-Type: application/json' --data @examples/test-request.json
```

## 自动化测试

```sh
go vet ./...
go test ./... -count=1
```

验收场景与测试的对应关系：

| 验收项 | 测试 |
| --- | --- |
| 无末尾换行（保留/新增/移除/标记不符拒绝） | `TestNoTrailingNewlineRemove`、`TestAddTrailingNewline`、`TestRemoveTrailingNewline`、`TestNoNewlineMarkerMustMatchFile`、`TestAppendToNoNewlineFile(KeepNoNewline)` |
| 文件增删 | `TestNewFile*`、`TestDeleteFile`、`TestDeleteNoTrailingNewline`、`TestCreateRejectsExistingFile`、`TestDeletePatchMustRemoveAllLines` |
| 重叠 hunk | `TestOverlappingHunks`（拒绝）、`TestAdjacentHunksAreAllowed`（相邻放行） |
| 错误上下文 / 错误行号 / 拒绝模糊重定位 | `TestWrongContext`、`TestWrongLineNumber`、`TestNoFuzzyRelocation` |
| 失败后原目录逐字节不变 | `TestBatchAtomicity`（库层）、`TestApplyFailureLeavesDirectoryByteIdentical`（HTTP 端到端，含二进制文件）、各失败用例的 `assertUnchanged` |
| 路径安全 | `TestInvalidPaths`、`TestApplyRejectsPathTraversal` |
| 缓存/工作目录分离 | `TestPublishCacheMustBeSeparate`、`TestApplyRejectsWorkdirContainingCache` |
| 发布期失败回滚 | `TestPublishRollback` |
| 测试夹具命令 | `TestRunFixtureCommand*`（仅执行显式 argv，回报退出码/输出） |

## 实测记录（2026-09-24，go1.22.2 linux/amd64）

以下命令在本仓库实际执行，输出为真实结果：

```text
$ gofmt -l .            # 无输出：全部文件已格式化
$ go vet ./...          # 通过，无告警
$ go test ./... -count=1
?       patchsvc/cmd/patchsvc   [no test files]
ok      patchsvc/internal/apply 0.027s
ok      patchsvc/internal/diff  0.002s
ok      patchsvc/internal/server 0.016s
# 共 43 个测试函数全部 PASS，0 FAIL，0 SKIP
$ go build -o bin/patchsvc ./cmd/patchsvc   # 成功
```

真实服务运行验证（`./bin/patchsvc -addr 127.0.0.1:8787 -cache-dir /tmp/patchsvc-demo/cache`）：

1. **失败批次**（`examples/apply-bad-request.json`，第二个补丁上下文错误）：
   返回 HTTP 422
   `{"ok":false,"error":"nonl.txt: hunk 1 context mismatch at old line 1: file has \"no newline file\", patch expects \"WRONG CONTEXT\""}`；
   失败前后 `find work -type f | sort | xargs sha256sum` 两次输出 `diff` 为空——
   **工作目录逐字节不变**。
2. **成功批次**（`examples/apply-request.json`，4 个文件补丁）：HTTP 200，
   `changed` 依次报告 `modify/create/delete/modify`；`f.txt` 内容正确替换，
   `sub/added.txt` 被创建，`old.txt` 被删除，`nonl.txt` 经 `xxd` 确认
   结尾无 `0a`（无末尾换行被准确保留）。
3. **测试夹具命令**（`examples/test-request.json`，`grep -n TWO f.txt`）：
   HTTP 200，`{"ok":true,"exit_code":0,"stdout":"2:TWO\n",...}`。
4. **路径穿越**：`+++ b/../escape.txt` → HTTP 422
   `path component ".." is not allowed in "../escape.txt"`，工作目录外无文件产生。
5. **重叠 hunk**：→ HTTP 422
   `f.txt: hunk 2 at old line 2 overlaps the previous hunk (which consumes up to line 3)`。
6. 缓存目录在请求结束后无残留（暂存目录随发布完成清理）。

未通过项：无。
