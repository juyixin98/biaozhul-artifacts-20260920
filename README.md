# localcache — 远程缓存本地模型（纯后端）

一个只在本地运行的“远程缓存”服务：内容寻址的 HTTP 制品缓存（CAS）+
动作缓存（AC）+ 显式夹具命令执行器，以及一个并发、校验摘要的客户端。
仅用 Go 标准库实现，**不连接任何云平台**，不做前端。

## 设计要点

- **缓存与工作目录分离**：缓存根目录（`-cache-dir`，含 `objects/`、`tmp/`、
  `ac/`）与构建工作目录根（`-work-dir`）必须是两个不同目录，启动时校验。
  每次构建在 `work-dir/build-*` 下新建临时目录，结束后删除。
- **原子发布**：上传先写入 `tmp/upload-*` 临时文件，边写边算 SHA-256，
  校验摘要与大小限制后 `fsync` 再 `rename(2)` 到 `objects/<digest>`。
  读者永远看不到部分写入的对象；中断的上传只残留临时文件，启动时清理。
- **大小限制**：`-max-object-size`（默认 64 MiB）。超限返回 `413`，
  已写临时文件删除，不发布。
- **客户端验证摘要**：下载比对 `Content-Length`（检测截断）与 SHA-256
  （检测损坏），失败按可重试错误重试（默认 2 次），最终报
  `truncated download` / `digest mismatch` 错误，绝不静默接受坏数据。
- **仅运行显式命令**：`/v1/builds` 只执行请求体里给出的 `argv`，
  服务自身不发明、不加载任何命令；环境变量最小化（PATH/HOME/TMPDIR），
  有超时上限，超时杀整个进程组。
- **坏缓存可诊断**：`GET /v1/admin/fsck` 重哈希所有已发布对象，报告
  内容损坏、残留临时文件、外来文件；CLI `fsck` 在不健康时退出码为 1。

## 目录结构

```
api/types.go            JSON 协议类型（服务端与客户端共享）
internal/cas/           内容寻址对象存储（原子发布、fsck）
internal/builder/       显式夹具命令执行器（工作目录与缓存分离）
internal/server/        HTTP JSON API（CAS + AC + builds + admin）
client/                 并发客户端（摘要校验、截断重试、PutMany/GetMany）
cmd/cacheserver/        服务可执行文件
cmd/cacheclient/        CLI 客户端
examples/requests.md    curl 请求样例
```

## 运行

```sh
go build ./...
go run ./cmd/cacheserver -addr 127.0.0.1:8080 \
    -cache-dir ./cache-data -work-dir ./work-tmp -max-object-size 67108864
```

CLI 客户端：

```sh
go run ./cmd/cacheclient -addr http://127.0.0.1:8080 put ./file.bin
go run ./cmd/cacheclient -addr http://127.0.0.1:8080 get <digest> ./out.bin
go run ./cmd/cacheclient -addr http://127.0.0.1:8080 build -in in.txt=<digest> -out out.txt -- sh -c 'tr a-z A-Z < in.txt > out.txt'
go run ./cmd/cacheclient -addr http://127.0.0.1:8080 stats
go run ./cmd/cacheclient -addr http://127.0.0.1:8080 fsck
```

## HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/v1/health` | 健康检查 |
| PUT | `/v1/cas/{sha256}` | 上传对象（body 为原始字节）；201 新建 / 200 去重；413 超限；400 摘要不匹配 |
| GET | `/v1/cas/{sha256}` | 下载对象（带 `Content-Length`、`ETag`）；404 不存在 |
| HEAD | `/v1/cas/{sha256}` | 存在性检查 |
| POST | `/v1/builds` | 运行显式夹具命令（动作缓存去重） |
| GET | `/v1/admin/stats` | 对象数、字节数、AC 条目数、大小上限 |
| GET | `/v1/admin/fsck` | 一致性扫描（损坏/残留/外来项） |

`POST /v1/builds` 请求体：

```json
{
  "argv": ["sh", "-c", "tr a-z A-Z < in.txt > out.txt"],
  "inputs": {"in.txt": "<sha256>"},
  "outputs": ["out.txt"],
  "timeout_ms": 5000
}
```

响应（第二次相同请求 `cache_hit: true`，命令不会重复执行）：

```json
{
  "action_digest": "...", "cache_hit": false, "exit_code": 0,
  "stdout": "", "stderr": "",
  "outputs": {"out.txt": "<sha256>"}, "duration_ms": 1
}
```

动作缓存键 = `{argv, inputs, 排序后的 outputs, timeout_ms}` 规范 JSON 的
SHA-256；AC 条目同样以临时文件 + rename 原子发布。相同的并发构建会被
串行化，夹具命令只跑一次。

## 测试

```sh
go vet ./...
go test -race -count=1 ./...
```

覆盖验收场景：

- **并发同摘要上传**（`internal/cas.TestConcurrentSameDigestUploads`、
  `client.TestConcurrentPutGetMany`）：32 个并发写同一摘要，全部成功，
  恰好发布一个完整对象，无残留临时文件。
- **截断下载**（`client.TestTruncatedDownloadDetectedAndRetried`）：
  连接中途断开被识别为截断并重试，恢复后拿到完整且摘要正确的字节；
  完整但损坏的下载被识别为摘要错误（`TestCorruptDownloadDetected`）。
- **截断上传**（`internal/server.TestTruncatedUploadIsNotPublished`、
  `cas.TestPutAbortedReaderPublishesNothing`）：中途断开的上传永不发布。
- **服务重启**（`cas.TestRestartPersistenceAndTmpCleanup`、
  `server.TestRestartAcrossServerInstances`）：已发布对象保留，
  残留临时文件被清理，部分对象不可见。
- **坏缓存可诊断**（`cas.TestFsckDiagnosesCorruption`、
  `server.TestFsckEndpointDiagnosesCorruption`）：磁盘上被篡改的对象
  被 fsck 点名，客户端下载也会拒绝。

## 实测记录（2026-09-24，go1.22.2 linux/amd64）

```
$ gofmt -l . && go vet ./... && go build ./...   # 无输出，全部通过
$ go test -race -count=1 ./...
ok  localcache/client            0.352s   (7 个测试)
ok  localcache/internal/cas      0.054s   (7 个测试)
ok  localcache/internal/server   0.236s   (7 个测试)
```

21 个测试全部通过，无未通过项。

真实服务演练（`cacheserver -addr 127.0.0.1:36633 -cache-dir … -work-dir …
-max-object-size 1048576`，详见 `examples/requests.md`）：

- 上传 18 字节对象 → `201`；重复上传 → `200 {"dedup":true}`；
  下载字节一致；HEAD 200；缺失对象 → `404 {"error":"cas: object not found"}`。
- 2 MiB 上传（限 1 MiB）→ `413`；错误摘要上传 → `400`，对象未发布。
- 构建 `tr a-z A-Z < in.txt > out.txt` → 输出对象 `aaad2e09…`，与本地
  `sha256sum` 一致；相同请求第二次 `cache_hit:true`、`action_digest` 相同。
- 杀掉服务重启 → 对象仍可下载（HTTP 200）；手工放入 `tmp/` 的残留文件
  在启动时被清理（`ls cache/tmp | wc -l` → 0）。
- 篡改磁盘上的对象 → `fsck` 报 `ok:false` 并列出损坏摘要；
  CLI 客户端下载该对象报 `digest mismatch on download` 并以退出码 1 失败；
  `cacheclient fsck` 退出码 1。

### 开发中发现并修复的问题（如实记录）

1. 初版超时测试耗时 31s：`sh -c 'sleep 30'` 被杀后，子进程 `sleep` 仍持有
   管道写端，`cmd.Wait()` 阻塞。修复：构建执行器使用独立进程组
   （`Setpgid`），超时时 `kill -pgid` 杀整组，并设 `WaitDelay` 上限。
   修复后服务器测试套件从 31s 降至 ~1.3s。
2. 初版截断下载测试的重试次数断言写错（期望 3 次调用，实际无重试场景下
   第二次 Get 一次成功，共 2 次）。修正断言。

## 限制

- 单机本地模型：无鉴权、无 TLS、无分布式；监听地址请绑定回环或受信网络。
- 对象整体读入内存的路径：构建输出收集与客户端 `Get`（受大小上限约束）。
- 进程组杀除使用 Unix 专有调用（`syscall.Kill(-pgid)`），仅支持类 Unix。
- 无垃圾回收：对象只增不减，需自行清理缓存目录。
