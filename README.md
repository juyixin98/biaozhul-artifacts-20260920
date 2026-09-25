# buildprov — 构建输入溯源（Build Input Provenance）

纯后端、纯本地的构建工程服务：用**声明式构建动作**描述构建，为每个制品记录
输入来源图（源码摘要、工具摘要、上游制品证明），支持

- **独立重算核验**传递来源（不信任任何已存索引/记录字段，所有 blob 重新读盘
  重新哈希，工具从磁盘文件重新解析并重新摘要，记录哈希与 HMAC 全部重算）；
- **影响查询**：某个源码（按精确内容摘要或按逻辑路径）变更会影响哪些输出；
- **证明链检测**：缺失 blob、内容篡改、证明记录篡改、上游记录哈希掉包、
  伪造循环、工具（解释器/夹具脚本/命令/环境）漂移；
- 明确区分**完整溯源（provenance complete）**与**结果可复现（reproducible）**。

无云连接、无前端。服务只监听回环地址；动作只能执行 fixtures 目录内的 bash
夹具脚本；构建在一次性私有工作目录中运行，源码缓存、制品缓存、工作目录三者
物理分离。

---

## 1. 构建与运行

要求 Go 1.22+（HTTP 路由使用 `method pattern`）。

```bash
go build -o bin/buildprov ./cmd/buildprov

./bin/buildprov \
  -addr 127.0.0.1:18791 \
  -data-dir /tmp/bp/data \
  -work-dir /tmp/bp/work \
  -fixtures ./fixtures
```

启动参数（均有默认值）：

| 参数 | 默认 | 含义 |
|---|---|---|
| `-addr` | `127.0.0.1:8080` | 监听地址，默认仅回环 |
| `-data-dir` | `./.bpdata` | 源码 CAS、制品 CAS、证明日志、HMAC 密钥、索引 |
| `-work-dir` | `<data-dir>/work` | 一次性构建工作目录（与两个缓存强制不同路径） |
| `-fixtures` | `./fixtures` | 唯一允许执行的脚本目录 |
| `-interpreter` | `/usr/bin/bash` | 唯一允许直接 exec 的程序 |

首次启动在 data-dir 生成 `hmac.key`（0600，32 字节随机），用于证明记录的
HMAC。证明日志是权威历史；`index/*.json` 只是可重建的查找缓存。

## 2. 快速体验

```bash
# 终端 A：启动（见上）

# 终端 B：端到端演示（注册→构建→核验→影响→可复现性对比→篡改检测）
# 第二个参数是服务的 -data-dir（默认 /tmp/bpdemo/data），第 7 步直接在该
# 目录中篡改一个制品 blob 以演示检测。
./scripts/demo.sh http://127.0.0.1:18791 /tmp/bp/data
```

## 3. 测试

```bash
go test ./...            # 单元 + 验收测试（会真实执行 bash 夹具）
go test -race ./...      # 竞态检测
```

测试覆盖的验收场景见 `internal/service/service_acceptance_test.go`：

| 测试 | 验收点 |
|---|---|
| `TestCompleteProvenanceVerifies` | 多级制品链独立核验通过 |
| `TestIndependentRecomputation` | 从原始字节重算摘要，校验传递来源 |
| `TestSharedDependencyImpact` | 共享依赖 common.js 变更影响全部下游；appA 不波及 appB；新摘要下旧制品被视为过期 |
| `TestMissingArtifactBlobDetected` | 删除 CAS blob → `ARTIFACT_BLOB_MISSING` |
| `TestTamperedArtifactBlobDetected` | 替换 CAS blob 内容 → 制品与下游输入双重摘要不匹配 |
| `TestTamperedAttestationRecordDetected` | 改证明内容 → 记录哈希不符；篡改磁盘日志后**新进程拒绝打开** |
| `TestForgedUpstreamHashDetected` | 用有效 HMAC 重签掉包后的上游哈希 → 交叉核验仍失败 |
| `TestForgedCycleDetected` | 伪造 A↔B 循环 → `CYCLE_DETECTED`，且拒绝在环上继续构建 |
| `TestToolDigestDriftDetected` | 改动夹具脚本 → 工具摘要漂移；同名工具禁止以不同摘要重复注册 |
| `TestReproducibleVersusNonDeterministic` | 嵌入时钟的动作：溯源完整但**不可复现** |
| `TestDisallowedCommandsRejected` | 非 bash 解释器、`..` 穿越、绝对路径脚本一律拒绝 |
| `TestWorkDirIsSeparateFromCaches` | 构建后工作目录清空；源码/制品缓存分离 |
| `TestOnChangeDoesNotDeadlock` | 状态变更回调在锁外触发（曾自死锁的回归测试） |
| `TestReRegisterSameToolIdempotent` | 相同动作定义重复注册幂等 |

实际运行记录（命令、输出、失败与修复）见 [`docs/RUNLOG.md`](docs/RUNLOG.md)。

---

## 4. 数据模型与证明结构

```
Source(path) ──digest──┐
                       ├─▶ InputRef(slot, kind, digest)
Artifact(upstream) ────┘                 │
                                         ▼
                 Tool(name, argv, env, interpreterDigest, scriptDigest)
                                         │ digest = canonical-JSON sha256
                                         ▼
                         Action(toolDigest, inputs[], outputs[])
                                         │ 执行夹具（一次性 workdir）
                                         ▼
                              Artifact (内容存入制品 CAS)
                                         │
                                         ▼
          Record（每个制品一条证明，JSONL 追加日志）
```

每条 `Record` 包含：

- `toolDigest`：命令 argv、静态环境、**解释器二进制字节摘要**、**夹具脚本
  字节摘要**的规范化 JSON 哈希——任何一个字节变化都会漂移；
- `inputs[]`：每个输入槽绑定来源（source 逻辑路径或上游 artifactID）与当时
  的内容摘要；
- `outputDigest`：制品实际字节摘要（构建后由服务从输出文件重算，而非信任
  脚本声明）；
- `upstreams`：`artifactID → 上游记录哈希`，形成**传递证明链**；
- `recordHash`：排除自身 `recordHash/sig` 后的规范 JSON SHA-256；
- `prev`：上一条记录哈希（创世 = sha256("")），形成**追加日志链**；
- `sig`：`HMAC-SHA256(key, recordHash)`。

ID 也是内容派生的：`art_<sha256(toolDigest, inputs, outputSlot)>`，
`act_<sha256(toolDigest, inputs, outputs)>`——同样的动作同样的输入得到同一
个制品 ID（天然缓存键，也使非确定性动作的重复构建可被识别）。

规范 JSON（`internal/canonical`）：键按字典序、无多余空白，保证不同进程/
Go map 迭代顺序下摘要一致。

### 安全边界

- 只 exec 配置的 bash；夹具参数必须是 fixtures 目录内、解析符号链接后仍不
  逃逸的相对路径（无 `..`、非绝对路径）；
- 工作目录每次 `MkdirTemp` 私有，固定最小 `PATH`，仅透传白名单环境变量，
  输入按 `$IN_<SLOT>`、输出按 `$OUT_<SLOT>` 注入，成功后删除；
- 执行前先在**已存在的证明图**上做环检测（正常追加不可能成环，因为输入必须
  先存在；此检测专门针对被伪造的存储）；
- 记录追加是“prev 必须等于当前链头”，防止分叉；重复 artifactID 拒绝追加；
  每行 fsync。

---

## 5. HTTP/JSON API

所有请求/响应均为 JSON（制品内容下载除外）。基址 `http://127.0.0.1:<port>`。

| 方法与路径 | 说明 |
|---|---|
| `GET  /v1/health` | 健康检查 |
| `POST /v1/sources` | 注册/更新逻辑路径下的源码内容 `{path, content}` |
| `GET  /v1/sources` | 列出源码 |
| `GET  /v1/sources/{path...}` | 查询单个源码 |
| `POST /v1/tools` | 注册（不可变）声明式动作 `{name, command[], env?}` |
| `GET  /v1/tools` / `GET /v1/tools/{name}` | 工具查询 |
| `POST /v1/actions` | 执行动作 `{tool, inputs:[{slot, sourcePath|artifactId}], outputs[]}` |
| `GET  /v1/artifacts` | 列出制品 |
| `GET  /v1/artifacts/{id}` | 制品元数据 |
| `GET  /v1/artifacts/{id}/content` | 制品字节（响应头 `X-Content-Digest`） |
| `POST /v1/artifacts/{id}/verify` | **独立重算核验**，200=完整，422=有 issue |
| `POST /v1/artifacts/{id}/reproduce` | **重跑动作**对比字节，区分溯源/可复现 |
| `GET  /v1/artifacts/{id}/provenance` | 传递来源树 |
| `GET  /v1/impact?source=<path>[&digest=<sha256:..>]` | 源码变更影响面 |
| `GET  /v1/records` / `GET /v1/records/{id}` | 证明日志 |

请求体样例在 [`examples/`](examples/)，真实响应样例（带 `_comment` 说明）在
同目录的 `*.response.json`。

### 调用序列示例

```bash
B=http://127.0.0.1:18791

# 1) 注册源码
curl -s -X POST $B/v1/sources -H 'Content-Type: application/json' \
  -d @examples/01-register-source.json

# 2) 注册动作（只接受 fixtures 内脚本）
curl -s -X POST $B/v1/tools -H 'Content-Type: application/json' \
  -d @examples/02-register-tool.json

# 3) 用源码构建库
curl -s -X POST $B/v1/actions -H 'Content-Type: application/json' \
  -d @examples/03-action-from-sources.json

# 4) 用上一步制品构建应用（把 art_... 替换为实际 ID）
curl -s -X POST $B/v1/actions -H 'Content-Type: application/json' \
  -d @examples/04-action-with-upstream.json

# 5) 独立核验 / 重算复现 / 影响 / 来源树
curl -s -X POST $B/v1/artifacts/art_xxx/verify    -d '{}'
curl -s -X POST $B/v1/artifacts/art_xxx/reproduce -d '{}'
curl -s "$B/v1/impact?source=src/common.js"
curl -s  $B/v1/artifacts/art_xxx/provenance
```

### verify 可能返回的 issue code

`ARTIFACT_NOT_FOUND`、`ARTIFACT_BLOB_MISSING`、`ARTIFACT_DIGEST_MISMATCH`、
`RECORD_NOT_FOUND`、`RECORD_HASH_MISMATCH`、`RECORD_SIG_INVALID`、
`LOG_CHAIN_BROKEN`、`CYCLE_DETECTED`、`TOOL_NOT_FOUND`、`TOOL_DIGEST_MISMATCH`、
`SOURCE_NOT_FOUND`、`INPUT_BLOB_MISSING`、`INPUT_DIGEST_MISMATCH`、
`UPSTREAM_RECORD_HASH_MISMATCH`、`UPSTREAM_RECORD_MISMATCH`、
`RECORD_OUTPUT_DIGEST_MISMATCH`、`OUTPUT_SIZE_MISMATCH`。

---

## 6. “完整溯源”与“可复现”是两件事

- **完整溯源**回答：记录是不是真的、链是否完整且与现存字节一致？
  时间敏感动作也可以拥有完全真实的溯源（记录了真实的输入、工具与输出摘要）。
- **结果可复现**回答：用同样的工具与同样的输入**再执行一次**，能否得到逐字节
  相同的输出？嵌入时钟、随机数、网络数据的动作溯源完整但不可复现。

`fixtures/nondet.sh` 专门演示这一点：它在输出里写入 UTC 时间戳，
`verify.complete=true` 而 `reproduce.reproducible=false`。

---

## 7. 目录结构

```
cmd/buildprov/            服务入口
internal/canonical/       确定性 JSON 编码
internal/provenance/      领域模型、CAS、源码/制品索引、追加证明日志
internal/executor/        夹具白名单策略 + 隔离进程执行
internal/service/         注册/执行/核验/复现/影响/来源图
internal/httpapi/         JSON HTTP API
fixtures/                 仅有的可执行构建脚本（compile_lib/link_app/nondet）
testdata/sources/         演示用源码（common.js 为共享依赖）
examples/                 请求体与真实响应样例
scripts/demo.sh           端到端演示脚本
docs/RUNLOG.md            实际运行记录（含失败项）
```

## 8. 范围与非目标

- 仅运行用户显式提供的夹具命令；不提供任意命令执行、不联网、无任何云平台
  集成、无前端、无鉴权层（仅回环监听，按本地工具定位）。
- 证明 HMAC 是服务本地密钥，提供篡改检测而非跨组织信任；跨组织场景需要换成
  非对称签名（模型已把“记录哈希”与“签名”分离，替换签名原语不影响链结构）。
