# modelcache — 远程缓存本地模型（纯后端）

一个**纯本地、纯后端**的模型制品缓存与构建工程服务。Go 实现，无云平台依赖、
无前端。构建服务**只运行操作者在夹具清单（fixture manifest）中显式发布的
命令**；HTTP API 调用方只能选择夹具名称，永远不能提交命令行。缓存目录与构建
工作目录严格分离。

## 它解决什么问题

- 内容寻址（SHA-256）的制品缓存：相同内容只存一份。
- 上传**先写临时对象、校验摘要后原子发布（rename）**：任何 GET 都不可能读到
  半成品对象。
- 客户端每次下载都**验证摘要**；下载中断时保留 `.part` 并以 HTTP `Range`
  断点续传，完成后原子落盘。
- 单对象大小上限；坏缓存可诊断（`/verify`、`cachectl doctor`）并自动隔离到
  `quarantine/`。
- 服务重启后对象持久，启动时清扫上次崩溃残留的 `tmp/` 暂存文件。

## 目录布局

```
digest/      SHA-256 摘要类型（sha256:<hex>）
store/       本地对象存储：tmp 暂存 + 原子 rename 发布 + 隔离区 + 启动清扫
cacheapi/    缓存 HTTP 服务（PUT/GET/HEAD/Range/verify/DELETE/stats）
client/      并发安全 Go 客户端：摘要校验、断点续传、重试、原子落盘
buildsvc/    构建服务：仅执行夹具白名单命令，工作目录与缓存分离
cmd/
  cacheserver  缓存服务进程
  buildserver  构建服务进程
  cachectl     缓存运维 CLI
  buildctl     构建服务 JSON 客户端
fixtures/     操作者显式提供的测试夹具（清单 + 脚本）
scripts/acceptance.sh   端到端验收脚本
```

### 缓存磁盘布局（`-cache-dir`）

```
<cache-dir>/
  blobs/sha256/<xx>/<64hex>   已发布对象（不可变，0444）
  tmp/                        在传暂存对象（对外不可见）
  quarantine/                 校验失败/损坏隔离对象（供诊断）
```

构建工作目录（`-work-dir`）和本地制品副本目录（`-artifact-dir`）与
`<cache-dir>` 是完全独立的目录树。

## 构建

```bash
go build ./...
go test ./...            # 单元/并发/中断测试（建议 -race）
go test -race ./...
```

## 快速开始（两个进程）

```bash
# 终端 1：缓存服务（缓存放在 ./cache，单对象上限 100MiB）
go run ./cmd/cacheserver -addr :8080 -cache-dir ./cache

# 终端 2：构建服务（工作目录 ./work，与缓存分离；只认 fixtures/manifest.json）
go run ./cmd/buildserver -addr :8081 \
  -cache-url http://127.0.0.1:8080 \
  -fixtures fixtures/manifest.json \
  -work-dir ./work -artifact-dir ./artifacts
```

## 使用 CLI

```bash
# 缓存
go run ./cmd/cachectl put    ./some-model.bin      # 计算 sha256 并上传
go run ./cmd/cachectl list
go run ./cmd/cachectl stat   sha256:<hex>
go run ./cmd/cachectl get    sha256:<hex> out.bin  # 校验+可续传下载
go run ./cmd/cachectl verify sha256:<hex>         # 服务端重算摘要
go run ./cmd/cachectl doctor                      # 全量体检
go run ./cmd/cachectl stats
go run ./cmd/cachectl delete sha256:<hex>

# 构建（只能选清单里的夹具名）
go run ./cmd/buildctl fixtures
go run ./cmd/buildctl start make-model
go run ./cmd/buildctl wait  <job-id> 60s
go run ./cmd/buildctl list
```

原始 HTTP 请求样例见 [`examples/requests.md`](examples/requests.md)。

## HTTP 接口

### 缓存服务（默认 :8080）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 健康检查 |
| GET | `/v1/blobs` | 列出对象 |
| GET | `/v1/stats` | 统计（对象数、字节数、tmp/隔离数量、大小上限）|
| PUT | `/v1/blobs/{sha256:hex}` | 上传（请求体即对象字节）|
| HEAD | `/v1/blobs/{digest}` | 仅元数据（`X-Content-Length`、`ETag`）|
| GET | `/v1/blobs/{digest}` | 下载，支持 `Range`、`ETag`/304 |
| POST | `/v1/blobs/{digest}/verify` | 重算摘要；损坏则移入隔离 |
| DELETE | `/v1/blobs/{digest}` | 淘汰对象 |

关键响应头：`ETag`（引号包裹的 hex）、`X-Content-Digest`、
`X-Content-Length`、`Accept-Ranges: bytes`。

错误统一为 JSON：`{"error":..., "code":..., "detail":...}`，`code` 取值
`not_found | object_too_large | digest_mismatch | size_mismatch | corrupt |
bad_digest | method_not_allowed | internal`。

### 构建服务（默认 :8081）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 健康检查 |
| GET | `/v1/fixtures` | 列出允许的夹具 |
| POST | `/v1/builds` | `{"fixture":"<name>"}` 启动一次构建 |
| GET | `/v1/builds` | 列出作业 |
| GET | `/v1/builds/{id}` | 查询作业 |
| GET | `/v1/builds/{id}?wait=30s` | 长轮询直到终态 |

作业终态 `succeeded | failed`，失败带 `exitCode`、`error`、`logsTail`；
成功带 `artifacts[].digest/size`。**夹具失败不会写入任何缓存对象。**

## 安全模型：只运行显式夹具

- 可执行的命令**只能**来自 `fixtures/manifest.json`，由操作者在服务启动时
  通过 `-fixtures` 指定。
- API 请求体只有夹具名；清单加载时校验名字、禁止带空格的 `command`
  （参数必须放在 `args`）、禁止 `..`/绝对路径输出。
- 夹具脚本可用 `${FIXTURES_DIR}` 占位符，加载时解析为清单所在目录的绝对路径，
  与作业工作目录无关。
- 每个作业在独立 `jobs/<id>/` 目录运行，独立进程组（超时整组杀掉），
  使用最小化环境变量；输出路径再次校验并限制在作业目录内。

## 一致性与中断处理

- **原子发布**：上传写入 `tmp/upload-*.part`（0600）→ 流式 SHA-256 →
  摘要相符才 `chmod 0444`、`fsync`、`rename` 到 `blobs/...` 并 fsync 父目录。
- **同摘要并发**：存储层按摘要 singleflight，多个相同上传只有一个领导者
  暂存字节，其余等待后命中已发布对象；最终 `blobs/` 中恰有一个对象。
- **坏上传**：摘要不符 → 400 `digest_mismatch`，字节移入 `quarantine/`，
  绝不发布。
- **下载中断**：客户端保留 `dst.part`，用 `Range: bytes=<offset>-` 续传，
  对已保留字节预置哈希；服务端若忽略 Range（回 200）则丢弃 part 整包重下；
  完整内容哈希不符则删除 part 整包重取一次；成功后 `fsync` + 原子 rename 到
  `dst`。
- **重启**：已发布对象持久；启动 `Sweep()` 清理 `tmp/` 孤儿文件并在日志记录。
- **坏缓存诊断**：`POST /verify` 或 `GET ...?verify=1` 重算摘要，损坏对象
  （如操作者拷入的坏目录、位翻转）返回 500 `corrupt` 并移入隔离区，
  `cachectl doctor` 可批量体检。

## 端到端验收

```bash
./scripts/acceptance.sh
```

覆盖：夹具白名单与命令注入拒绝、成功/失败构建、客户端摘要校验、同摘要
20 路并发上传恰好一个对象、上传期间持续探测不出现半成品、截断下载与
Range 续传、2MiB 大小上限、损坏对象诊断与隔离自愈、**服务重启持久化 +
tmp 清扫**、缓存/工作目录分离、stats 计数。端口自动选择空闲端口，也可用
`CACHE_PORT` / `BUILD_PORT` 覆盖。

## 范围与非目标

- 无前端、无 UI。
- 不连接任何云平台 / 对象存储；全部本地文件系统与 HTTP。
- 不做鉴权 / TLS（面向本机/受控网络）；如需暴露请置于反向代理之后。
