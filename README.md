# 镜像多架构选择服务（OCI Multi-Architecture Platform Selection）

纯后端服务：解析**本地** OCI 镜像索引（image index），按 `os / architecture / variant`
精确选择目标镜像，并对索引、（可嵌套的）子索引、manifest、image config 以及每一层的
**摘要（digest）和大小（size）逐一做真实校验**。不访问任何公网镜像仓库。

技术栈：**Go 1.22 · chi · SQLite（纯 Go 驱动 modernc.org/sqlite，无需 CGO）**。

---

## 1. 它解决什么问题

多架构镜像通过一个 *image index* 指向多个平台的 *image manifest*。本服务在本地完成：

1. **遍历解析**：从根索引开始，支持任意层嵌套索引，直到叶子 manifest。
2. **平台选择**：按 `os`、`architecture`、`variant` 匹配，规则明确、可预测；
   出现多个候选时返回 **409 歧义**，绝不随机挑一个。
3. **完整性校验**：链路上每个 blob 都按真实字节重算 SHA-256/SHA-512，并比对声明的
   digest 与 size；config 里的平台还会和索引中广告的平台、请求平台交叉核对。
4. **可变标签 + 不可变任务**：标签（tag）可以移动，但一次解析任务在创建时就把 tag
   **快照绑定到 digest**；标签随后移动不影响历史任务及其完整依赖链。

> 同样的标签名在不同仓库里是**不同产物**；同一标签移动前后也是**不同产物**。
> 身份永远来自内容 digest，而不是名字。

---

## 2. 快速开始

```bash
# 1) 生成示例 OCI 仓库（真实哈希，不联网）
make fixtures                       # 或: go run ./cmd/genfixtures -registry ./registry

# 2) 启动服务
go run ./cmd/server -addr :8080 -registry ./registry -db ./data/ocimp.db

# 3) 健康检查
curl -s http://127.0.0.1:8080/healthz
```

依赖已 **vendor**（`vendor/`），可用 `GOPROXY=off go build ./...` 完全离线构建；
版本同时锁定在 `go.mod` / `go.sum`。

### 一键验收（推荐）

```bash
make acceptance        # 或: ./scripts/acceptance.sh
```

该脚本会自行构建、生成 fixture、在临时端口起服务并断言全部场景，结束后自动清理。
输出末尾应为 `ALL ACCEPTANCE CHECKS PASSED`。

### 单元测试

```bash
make test              # go test ./...
make race              # go test -race ./...   （竞态检测）
make cover             # 覆盖率
```

---

## 3. 一个完整调用

选择 `demo/two-arch` 的 `linux/arm64`：

```bash
curl -s -XPOST http://127.0.0.1:8080/v1/repos/demo/two-arch/resolve \
  -H 'Content-Type: application/json' \
  -d @examples/resolve-linux-arm64.json
```

请求体（见 `examples/`）：

```json
{ "reference": "latest",
  "platform": { "os": "linux", "architecture": "arm64" } }
```

成功返回 `201`，关键字段：

```jsonc
{
  "id": "d6363b43-…",                 // 任务 ID，可用于 GET 历史
  "status": "succeeded",
  "reference": {
    "requested": "latest",
    "resolvedDigest": "sha256:9de8…", // 解析时把标签快照到的根索引 digest
    "boundFromTag":   "sha256:9de8…"
  },
  "resolved": {
    "manifest": { "digest": "sha256:fec5…", "size": 475, "mediaType": "…manifest.v1+json" },
    "config":   { "digest": "sha256:3d0d…", "size": 191, "mediaType": "…config.v1+json" },
    "layers":   [ { "digest": "sha256:9588…", "size": 84,  "mediaType": "…tar+gzip" } ],
    "platform": { "os": "linux", "architecture": "arm64", "variant": "" },
    "rootDigest": "sha256:9de8…"
  },
  "dependencyChain": [               // 完整依赖链，根在前
    { "position": 0, "role": "index",    "digest": "sha256:9de8…", "size": 645 },
    { "position": 1, "role": "manifest", "digest": "sha256:fec5…", "size": 475 },
    { "position": 2, "role": "config",   "digest": "sha256:3d0d…", "size": 191 },
    { "position": 3, "role": "layer",    "digest": "sha256:9588…", "size": 84  }
  ]
}
```

失败同样会落库一条任务，并返回结构化错误码，例如：

```json
{ "status": "failed",
  "id": "70ed5aca-…",
  "error": { "code": "platform_ambiguous",
             "detail": "platform linux/amd64 is ambiguous: 2 candidates: sha256:1d63…, sha256:2212…" } }
```

---

## 4. 平台选择规则（明确、无随机）

按顺序执行：

1. **os 与 architecture 必须精确相等**（大小写敏感），不匹配直接 `no_platform_match`。
2. 请求**显式给了 variant** → candidate 的 variant 必须**逐字符精确相等**；
   没有任何候选项就是硬未命中，**不会**回退到其它变体。
3. 请求**省略 variant**：
   1. 优先选“索引里 variant 也为空”的候选（请求不指定 ⇒ 优先未声明）；
   2. 若不存在，则应用该架构文档化的**默认 variant**：`arm → v7`、`arm64 → v8`；
   3. 没有默认值的架构只接受空 variant。
4. 去重后（同一 digest 的多条描述符视为同一产物，不算歧义）：
   - 恰好一个 → 选中；
   - **两个及以上不同 digest → `platform_ambiguous`（HTTP 409），拒绝猜测**。

这些规则在 `internal/selector/selector.go` 与 `selector_test.go` 中逐条可测。

---

## 5. 完整性与密码学校验（真实执行）

所有摘要都用标准库 `crypto/sha256`、`crypto/sha512` 对**实际字节**计算，不信任任何
描述符字段（见 `internal/digest/digest.go`）：

- 读取 blob 时**流式重算哈希**，同时核对 `digest` 与 `size`；
- 索引 / 嵌套索引 / manifest / config / **每一层**全部覆盖；
- blob 的 JSON 类型由其真实结构（含 `manifests` 还是 `config` 字段）判定，而非只信
  `mediaType` 字符串；
- image config 中的 `os/architecture/variant` 是权威来源，会与索引广告值、请求值双向核对；
- 任何不一致返回明确错误码：`digest_mismatch` / `size_mismatch` /
  `config_platform_mismatch` / `blob_missing`。

解析器对存储的身份以“**字节实际哈希到的 digest**”为准（`ContentStore.ReadVerified`
会回传真实身份），因此一个用别名把别的内容冒充成某 digest 的存储无法借此隐藏引用环。

### 关于循环引用

在一个严格的内容寻址存储中，真实环在数学上不可构造（需要 SHA-256 不动点）。
因此：

- 对正常存储，尝试制造别名环会先被 **`digest_mismatch`** 拦截；
- 解析器仍内置基于 **DFS 路径上内容身份** 的环检测作为纵深防御，当存储层因缺陷
  用别名提供了别的 blob 的字节时返回 **`cycle_detected`**。

两种路径都在 `internal/resolver/cycle_test.go` 中用真实哈希验证
（自引用、A↔B 互指两个用例）。

---

## 6. 标签移动与任务绑定

标签是可变指针，存在 SQLite；解析是一次性、不可变的历史记录。

```bash
# 1) 先用 latest（指向 v1）解析一次，记录返回的 id
# 2) 把 latest 移动到 v2 的索引 digest
curl -s -XPUT http://127.0.0.1:8080/v1/repos/demo/tag-move/tags/latest \
  -H 'Content-Type: application/json' \
  -d '{"digest":"sha256:72522f9d…","mediaType":"application/vnd.oci.image.index.v1+json"}'

# 3) 之后新的解析跟随新 digest；而旧任务 GET 回来仍绑定移动前的 digest 与依赖链
curl -s http://127.0.0.1:8080/v1/repos/demo/tag-move/tasks/<旧任务id>
```

服务启动时会从每个仓库的 `index.json`（`org.opencontainers.image.ref.name` 注解）
**播种**标签，但**绝不覆盖**数据库里已存在的标签——这样通过 API 移动过的标签重启后
依然保持。

---

## 7. HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET  | `/healthz` | 健康检查 |
| GET  | `/v1/repos` | 列出本地仓库 |
| GET  | `/v1/repos/{repo}/tags` | 列出标签（repo 可含 `/`） |
| PUT  | `/v1/repos/{repo}/tags/{tag}` | 移动/设置标签（body: `digest`，可选 `mediaType`） |
| POST | `/v1/repos/{repo}/resolve` | 平台解析，body 见下 |
| GET  | `/v1/repos/{repo}/tasks` | 任务历史（`?limit=`） |
| GET  | `/v1/repos/{repo}/tasks/{id}` | 单个任务（含完整依赖链） |
| GET  | `/v1/repos/{repo}/blobs/{digest}` | 读取已校验存在的 blob |
| PUT  | `/v1/repos/{repo}/blobs/{digest}?mediaType=…` | 上传 blob（服务端重算哈希，不一致返回 422） |

`POST /resolve` 请求：

```json
{ "reference": "latest | sha256:<hex>",
  "platform": { "os": "linux", "architecture": "arm64", "variant": "v8" } }
```

- `reference` 省略时默认 `latest`；以 `sha256:` / `sha512:` 开头则按 digest 直取，
  与标签解耦。
- 仓库名可含多段斜杠（如 `demo/two-arch`），由自定义分发器解析。

### 状态码 / 错误码

| HTTP | code | 含义 |
|---|---|---|
| 201 | — | 解析成功（任务已落库） |
| 400 | `invalid_reference` / `invalid_json` / `invalid_repository` / `invalid_tag` | 请求非法 |
| 404 | `tag_not_found` / `blob_missing` / `no_platform_match` | 引用、blob 或平台不存在 |
| 409 | `platform_ambiguous` | 多个候选，拒绝随机选择 |
| 422 | `digest_mismatch` / `size_mismatch` / `config_platform_mismatch` / `malformed_json` / `cycle_detected` / `unknown_manifest_type` | 内容存在但校验不通过 |

---

## 8. 示例仓库（fixtures）

`make fixtures` 生成下列仓库到 `registry/`（每个 blob 都是真实哈希）：

| 仓库 | 用途 |
|---|---|
| `demo/two-arch` | linux/amd64 + linux/arm64 两个架构 |
| `demo/arm-variants` | arm/v6、arm/v7、arm64/v8，验证 variant 规则 |
| `demo/ambiguous` | 两个不同的 linux/amd64 manifest → 歧义 |
| `demo/bad-config` | 索引广告 linux/arm64，但 config 实为 linux/386 → 平台造假 |
| `demo/missing-layer` | manifest 引用了一个已被删除的层 → 缺层 |
| `demo/tag-move` | 两个不同索引，演示标签移动与任务绑定 |

磁盘布局是标准 OCI image-layout：

```
registry/demo/two-arch/
├── oci-layout
├── index.json                 # 含 ref.name 标签注解
└── blobs/sha256/<hex>
```

---

## 9. 目录结构

```
.
├── cmd/
│   ├── server/          HTTP 服务入口（播种标签、优雅退出）
│   └── genfixtures/     生成真实哈希的示例仓库
├── internal/
│   ├── digest/          digest 解析 + 真实 SHA-256/512 校验
│   ├── oci/             OCI 索引/manifest/config 类型
│   ├── blobstore/       本地 OCI image-layout 内容存储
│   ├── selector/        平台精确选择 + 歧义判定（纯函数，易测）
│   ├── resolver/        遍历/校验/选择/依赖链/环检测
│   ├── store/           SQLite：标签与任务（含完整依赖链）
│   ├── fixture/         供测试与生成器使用的真实内容构建器
│   └── api/             chi 路由、请求处理、错误码映射
├── examples/            示例请求 JSON
├── scripts/acceptance.sh 一键端到端验收
├── registry/            生成的示例 OCI 仓库
├── vendor/              锁定依赖（可离线构建）
├── go.mod / go.sum
└── Makefile
```

---

## 10. 本地启动与验收命令汇总

```bash
go mod vendor                      # 固化依赖（仓库已附带 vendor/）
make fixtures                      # 生成示例输入（真实哈希）
make test                          # 全部单元测试
make race                          # 竞态检测
make acceptance                    # 端到端验收（推荐先跑这个）

go run ./cmd/server -addr :8080 -registry ./registry -db ./data/ocimp.db
# 离线验证：GOPROXY=off go build -mod=vendor ./...
```

## 11. 设计取舍与边界

- **纯本地、零公网**：只读取 blobstore 中存在的内容，不做任何 registry 拉取；
  描述符里的 `urls` 也不会被用于补取缺失 blob（缺层即 `blob_missing`）。
- **层内容不解压**：服务只验证层 blob 的摘要/大小（分发侧职责），`config.rootfs.diff_ids`
  在示例中按内容真实计算填充，但不要求与压缩层逐字节比对。
- **无鉴权 / 无 TLS**：定位为本地工具型后端，监听 loopback 即可；如需对外暴露应放在
  反向代理之后。
- 所有计算、JSON 解析、哈希与 SQLite 读写均真实执行；任何校验失败都会如实以结构化
  错误码与非 2xx 状态返回，并作为 `failed` 任务留痕。
