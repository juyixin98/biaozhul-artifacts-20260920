# ociarch — 镜像多架构选择服务

纯后端服务：解析**本地** OCI 镜像索引（image index），在导入时真实校验索引、
manifest、config 与每一层的 **sha256 摘要和字节大小**，并按
`os / architecture / variant` 做确定性的多架构 manifest 选择。

- 语言/框架：Go 1.22、Chi v5、SQLite（`modernc.org/sqlite`，纯 Go，无 cgo）
- 不拉取任何公网镜像：输入仅为本地 OCI layout 目录（`index.json` + `blobs/`）
- 标签是可移动指针；解析任务绑定**摘要（digest）**，相同标签绝不视为相同产物

## 构建与启动

```bash
go build ./...                 # 编译
go test ./...                  # 自动化测试（验收）
go run ./cmd/server -addr :8080 -db data/ociarch.db
```

依赖已锁定在 `go.mod` / `go.sum`（`github.com/go-chi/chi/v5 v5.0.12`，
`modernc.org/sqlite v1.29.10` 及其传递依赖）。

## 验收命令

```bash
# 1. 跑全部自动化测试（两架构、错误 config 平台、缺层、循环引用、歧义、标签移动）
go test ./...

# 2. 启动服务
go run ./cmd/server -addr :8080 -db data/ociarch.db &

# 3. 导入仓库内提交好的两架构示例（linux/amd64 + linux/arm64）
curl -s -X POST localhost:8080/api/v1/import -d @examples/import-request.json

# 4. 按平台解析（返回完整依赖链：index → manifest → config → layers）
curl -s -X POST localhost:8080/api/v1/resolve -d @examples/resolve-amd64.json
curl -s -X POST localhost:8080/api/v1/resolve -d @examples/resolve-arm64.json

# 5. 查询解析任务（含冻结的依赖链）与标签历史
curl -s localhost:8080/api/v1/resolutions/1
curl -s localhost:8080/api/v1/tags
curl -s localhost:8080/api/v1/tags/demo/app:1.0/history
```

`examples/oci-two-arch/` 是确定性的两架构 OCI layout 示例输入，可用
`go run ./cmd/fixturegen -out <dir> -case two-arch` 重新生成（内容逐字节一致）。
其它 fixture：`-case bad-config | missing-layer | ambiguous | arm-variants`。

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/import` | `{path, tag}`：校验并导入本地 OCI layout，tag 指向根摘要 |
| POST | `/api/v1/resolve` | `{ref, platform:{os,architecture,variant?}}`：平台选择，`ref` 为标签或 `sha256:` 摘要 |
| GET | `/api/v1/resolutions` / `/api/v1/resolutions/{id}` | 解析任务列表 / 详情（含冻结依赖链） |
| GET | `/api/v1/tags` | 标签 → 当前摘要 |
| GET | `/api/v1/tags/<name>/history` | 标签移动历史（name 可含 `/`） |
| GET | `/healthz` | 健康检查 |

错误一律以 JSON 返回：`{"error": {"code", "message"}}`。

## 导入时的真实校验（`internal/oci/layout.go`）

对每个对象流式计算 sha256 并与描述符比对，同时核对字节大小，任何不符即整体失败（HTTP 422）：

1. **index.json**：以其内容摘要作为根摘要；递归支持嵌套索引。
2. **manifest**：每个描述符的 digest + size 必须匹配 blob。
3. **config**：digest + size 校验后，还要求 config 内声明的
   `os/architecture/variant` 与索引描述符的 platform **完全一致**
   （`bad-config` fixture 即因此被拒绝）。
4. **每一层**：blob 必须存在且 digest + size 匹配（`missing-layer` fixture
   引用了一个不存在的层，导入被拒）。
5. **循环引用**：遍历携带 visiting 集合，导入后再对依赖图做一次
   `DetectCycle` 深度优先检查。说明：在内容寻址（digest = 内容哈希）下，
   构造一个能通过摘要校验的真实循环在计算上不可行（需要 sha256 不动点），
   因此循环检测以防损坏元数据图为目的，由 `internal/oci/graph_test.go`
   用合成图（自环、间接环、菱形 DAG）直接验证。

## 平台选择规则（`internal/oci/select.go`，确定性，绝不随机）

1. 先按 `os` + `architecture` **精确**过滤。
2. variant：
   - 请求带 variant → 只保留 variant 完全相等者；
   - 请求不带 variant → 优先无 variant 的 manifest；一个都没有时，
     回退到全部 os/arch 匹配者参与竞争。
3. 结果：0 个 → `404 no_match`；恰好 1 个 → 选中；
   **多于 1 个 → `409 ambiguous`**，错误中列出全部候选 digest 与 variant，
   并落库一条 `status=ambiguous` 的解析任务，绝不随机挑选。

## 标签可移动，任务绑定摘要

- `import` 时标签被更新为新的根摘要，并追加一行 `tag_history`；
- `resolve` 的瞬间把标签钉到当前摘要，解析任务记录 `root_digest`，
  并把当时解析出的完整依赖链（index → manifest → config → 各层，
  含 digest/mediaType/size）**复制**进 `resolution_chain`；
- 之后标签再移动、同一标签导入不同产物，都不影响已存在的任务——
  由 `TestTagMoveResolutionBoundToDigest` 验证：同一 `demo/app:latest`
  先后解析出两个不同 digest，旧任务的链保持不变。

## 目录结构

```
cmd/server/       HTTP 服务入口
cmd/fixturegen/   确定性 OCI layout 生成器（示例输入/测试共用）
internal/oci/     类型、摘要校验、layout 导入校验、平台选择、循环检测
internal/store/   SQLite：nodes/edges/tags/tag_history/resolutions/resolution_chain
internal/api/     Chi 路由与处理器
internal/fixture/ 测试 fixture 构建器
examples/         提交的示例 OCI layout 与请求 JSON
```
