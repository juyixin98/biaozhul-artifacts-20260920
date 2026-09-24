# OCI 多架构清单选择服务（Rust + Axum）

纯后端 HTTP 服务，模拟 OCI Image Spec / Distribution 的**多平台制品（manifest list / image index）选择**：
给定目标 `os / architecture / variant / features`，在索引树中递归查找、验证摘要、选出唯一匹配的子清单；
**候选并列时报歧义错误，绝不按文件顺序取第一个**。

- 语言/框架：Rust 2021 + [Axum](https://github.com/tokio-rs/axum) 0.8 + Tokio
- 摘要：`sha256`（OCI 唯一强制算法），上传与遍历两处都做内容哈希校验
- 存储：进程内、内容寻址（digest → bytes）+ 仓库标签表
- **不下载任何公网镜像**：数据只来自内置 fixture 和客户端 `PUT` 上传；代码中没有任何 HTTP 客户端依赖

---

## 1. 依赖与启动

### 依赖

- Rust/Cargo（开发用 1.98，edition 2021；`axum 0.8` / `tokio 1` / `serde 1` / `sha2 0.10` / `hex 0.4` / `thiserror 2` / `tracing`）
- 构建时需要 crates.io 访问（或本机镜像/缓存）。依赖版本锁定在 `Cargo.lock`。
- 运行时无外部服务、无网络、无数据库、无系统库。

### 启动

```bash
cargo run --release                          # 默认 127.0.0.1:8080，启动时装载内置 fixture
cargo run -- --port 9000 --host 0.0.0.0      # 自定义监听地址
cargo run -- --no-fixtures                    # 空仓库启动，只用客户端上传的数据
```

启动后：

- `GET /healthz` 健康检查
- `GET /v2/` Registry 版本探针
- 日志会打印装载的 fixture 标签

---

## 2. HTTP 接口

> 仓库名允许含斜杠（如 `demo/app`），因此 `/v2/*` 用通配路由 + 后缀分发（与真实 Registry 的路径模型一致）。

### 2.1 查询标签

```
GET /v2/{repo}/tags/list
```

### 2.2 上传 blob（单段式 monolithic upload）

```
PUT /v2/{repo}/blobs/uploads/?digest=sha256:<hex>
PUT /v2/{repo}/blobs/uploads/{uuid}?digest=sha256:<hex>
```

- Body 即 blob 原始字节；服务端重算 SHA-256，与 `digest` 参数不一致 → `400 DIGEST_MISMATCH`，拒绝写入。
- 成功返回 `201`、`Location: /v2/{repo}/blobs/<digest>`。

### 2.3 上传清单（image manifest 或 image index）

```
PUT /v2/{repo}/manifests/{tag}                 # 按标签上传
PUT /v2/{repo}/manifests/sha256:<hex>          # 按摘要上传（?strict=false 可放宽）
```

- `Content-Type` 取 `application/vnd.oci.image.manifest.v1+json` 或 `...index.v1+json`；缺省时按 JSON 中 `manifests`/`config` 字段推断。
- 引用的所有 blob 必须已存在，否则 `404 BLOB_UNKNOWN`。
- 按标签上传时，返回头 `Docker-Content-Digest` 给出内容摘要并建立 tag 映射。

### 2.4 拉取 blob / 清单

```
GET /v2/{repo}/blobs/sha256:<hex>
GET /v2/{repo}/manifests/{tag|digest}
```

### 2.5 ★ 多平台选择（核心接口）

```
POST /v2/{repo}/select[?strict=true|false]
Content-Type: application/json

{
  "reference": "latest",                       // 标签、摘要，或完整 repo:tag / repo@sha256:...
  "os": "linux",
  "architecture": "arm",
  "variant": "v7",                             // 可选
  "os_version": null,                          // 可选
  "os.features": [],                           // 可选
  "features": []                               // 可选 CPU 特性
}
```

成功 `200`：

```json
{
  "status": "selected",
  "result": {
    "reference": "demo/app:latest",
    "requested": { "...": "回显查询条件" },
    "selected": {
      "digest": "sha256:…",                    // 命中的叶子 image manifest
      "media_type": "application/vnd.oci.image.manifest.v1+json",
      "platform": { "os": "linux", "architecture": "arm", "variant": "v7" },
      "score": { "variant": 2, "features": 0 },
      "path": [                                // ★ 选择路径：根索引起每一跳
        { "index": 0, "digest": "sha256:…", "media_type": "...index.v1+json", "platform": null },
        { "index": 2, "digest": "sha256:…", "media_type": "...manifest.v1+json",
          "platform": { "os": "linux", "architecture": "arm", "variant": "v7" } }
      ]
    },
    "candidate_count": 1,
    "best_candidates": ["sha256:…"]
  }
}
```

错误统一为 OCI 风格信封 `{"errors":[{"code","message","detail"?}]}`：

| HTTP | code | 触发条件 |
| --- | --- | --- |
| 404 | `NO_MATCH` | 索引树中没有任何平台能满足查询（含“缺失平台”） |
| 409 | `AMBIGUOUS` | 两个及以上候选以相同的最高分数并列 |
| 422 | `DIGEST_MISMATCH` | 描述符声明的 digest/size 与实际字节不符 |
| 422 | `MANIFEST_CYCLE` | 子索引形成环（消息给出完整环路） |
| 422 | `INVALID_MANIFEST` | JSON 非法 / 结构不支持 |
| 404 | `BLOB_MISSING` / `BLOB_UNKNOWN` | 描述符指向的内容不存在 |
| 404 | `REFERENCE_NOT_FOUND` | tag/摘要引用不存在 |
| 400 | `INVALID_DIGEST` / `BAD_REQUEST` | digest 格式错或缺 os/arch |

`strict`（默认 `true`）遍历时对每个被跟随的清单重算 SHA-256 并核对描述符 `size`。
内置的“环”fixture 使用合成摘要键挂载（`sha256:aaa…`），需用 `?strict=false` 进入遍历以演示环检测；
而“损坏”fixture 用的是**真实摘要键 + 被篡改字节**，即使 strict 也必然报 `DIGEST_MISMATCH`。

### 2.6 管理接口

```
GET /admin/verify      # 对存储中每个 blob 重算 SHA-256，列出不匹配项
```

---

## 3. 选择算法（关键语义）

1. **解析引用**：`repo:tag` 或 `repo@sha256:…`，得到根清单。
2. **递归遍历索引树**（DFS），每跳记录到 `path`：
   - 跟随子描述符前，取 blob →（strict 且非合成键挂载时）重算 SHA-256、比对 `size`；
   - **环检测**：维护 DFS 活动路径集合，重复进入同一摘要立即报 `MANIFEST_CYCLE` 并回显环路；
   - 带 `platform` 的子项先做可满足性剪枝；无 `platform` 的嵌套索引总是进入。
3. **平台匹配与打分**（`os`/`architecture` 必须精确相等）：
   - **ARM 变体阶梯** `v8 > v7 > v6 > v5`：精确变体得 2 分；请求较新变体时可回退到较旧变体（v7→v6）得 1 分；较旧请求不能用较新清单（不兼容）。`arm64` 缺省视为 `v8`。
   - **CPU features**：候选要求的特性必须是目标特性的子集；精确相等 2 分，真子集 1 分，候选要求了目标没有的特性则淘汰。
   - `os.version` 请求指定时须精确相等；`os.features` 要求候选全部提供。
4. **绝不“取第一个”**：收集全部匹配叶子，按分数取最大值；若最高分有多个 → `409 AMBIGUOUS`。
   排序/并列判断只依赖平台条件与分数，与描述符在文件中的出现顺序无关（有测试用正序/逆序两个索引证明结果一致）。

---

## 4. 内置 Fixture（启动即有）

| 引用 | 内容 | 预期 |
| --- | --- | --- |
| `demo/app:latest` | 嵌套索引：arm/v5、v6、v7、arm64(v8)、amd64、s390x | 变体精确选择、回退、深路径 |
| `demo/ambiguous:1` | 两个 linux/amd64 叶子，条件完全相同 | `AMBIGUOUS` |
| `demo/features:1` | 无特性 / sse4_2 / sse4_2+avx 三个叶子 | 特性子集打分 |
| `demo/direct:1` | tag 直接指向 image manifest | 直接返回 |
| `demo/corrupt:1` | 叶子字节被篡改但保留真实摘要键 | `DIGEST_MISMATCH`（strict） |
| `demo/cycle:latest` | A、B 两索引互相指向（合成摘要键） | `MANIFEST_CYCLE`（strict=false） |

### curl 速查

```bash
# arm 变体（经嵌套索引，返回完整路径，score.variant=2 精确）
curl -s -X POST localhost:8080/v2/demo/app/select -H 'content-type: application/json' \
  -d '{"reference":"latest","os":"linux","architecture":"arm","variant":"v7"}'

# 缺失平台 -> 404 NO_MATCH
curl -s -X POST localhost:8080/v2/demo/app/select -H 'content-type: application/json' \
  -d '{"reference":"latest","os":"linux","architecture":"ppc64le"}'

# 相同条件多候选 -> 409 AMBIGUOUS
curl -s -X POST localhost:8080/v2/demo/ambiguous/select -H 'content-type: application/json' \
  -d '{"reference":"1","os":"linux","architecture":"amd64"}'

# 子清单环 -> 422 MANIFEST_CYCLE（strict=false，消息含 a -> b -> a）
curl -s -X POST 'localhost:8080/v2/demo/cycle/select?strict=false' \
  -H 'content-type: application/json' \
  -d '{"reference":"latest","os":"linux","architecture":"amd64"}'

# 引用摘要被篡改 -> 422 DIGEST_MISMATCH
curl -s -X POST localhost:8080/v2/demo/corrupt/select -H 'content-type: application/json' \
  -d '{"reference":"1","os":"linux","architecture":"amd64"}'
```

从零用 HTTP 上传 config/layer/manifest/index 再选择的完整脚本见
[`examples/upload_and_select.sh`](examples/upload_and_select.sh)：

```bash
./examples/upload_and_select.sh            # 默认 localhost:8080
BASE=http://127.0.0.1:9000 ./examples/upload_and_select.sh
```

---

## 5. 自动化测试

```bash
cargo test            # 13 个单元测试 + 14 个端到端集成测试
cargo clippy --all-targets   # 静态检查（交付时无警告）
```

覆盖的验收点：

- **arm 变体**：v7 精确命中、v7 请求在仅 v6 时回退、arm64↔v8、嵌套索引路径为 `根索引[0] → 叶子[2]`；
- **缺失平台**：请求 `linux/riscv64`、`linux/ppc64le` 时返回明确 `NO_MATCH`；
- **相同条件多候选**：同一对叶子分别按正序、逆序构造两个索引，均报 `AMBIGUOUS`（证明与顺序无关）；
- **子清单环**：A→B→A 返回 `MANIFEST_CYCLE` 且消息含完整环路；
- **引用摘要校验**：上传哈希不符 → 拒绝；存储篡改（真实键/错误内容）→ 选择时 `DIGEST_MISMATCH`；
  描述符 size 不符、blob 缺失分别报错；
- **HTTP 端到端**：经 Axum router 实际请求验证选择、409/404/422 状态码，以及完整“上传→选择”闭环。

---

## 6. 项目结构

```
Cargo.toml / Cargo.lock        # 依赖与锁定版本
src/
  main.rs        启动参数、fixture 装载、优雅关停
  lib.rs         模块导出
  digest.rs      sha256 解析/计算/校验
  model.rs       OCI descriptor / platform / manifest 类型与解析
  store.rs       内容寻址存储 + tag 表 + 全量校验
  selector.rs    匹配/打分/歧义/环检测（核心）
  fixtures.rs    内置演示仓库（真实哈希构造）
  api.rs         Axum 路由、通配路径分发、错误信封
tests/
  end_to_end.rs  14 个集成/HTTP 测试
examples/
  upload_and_select.sh         curl 全流程样例
```

---

## 7. 设计边界（如实说明）

- 只实现 `sha256`；OCI 虽允许其他算法，但所有实际制品均为 sha256。
- 存储是**进程内内存**：重启后除 fixture 外的数据丢失；无鉴权、无分片/断点上传（blob 用单段上传）、不分发层内容。
- “环”在真正的内容寻址存储里数学上不可能出现（一个内容无法包含自身的摘要）。
  为满足验收，环 fixture 用**显式的合成摘要键** `sha256:aaa…/bbb…` 挂载并须配合 `strict=false`；
  这是“挂载一个不受信任的本地布局进行检查”的模型，而不是从网络拉取。
- 未实现：OCI 鉴权（401/WWW-Authenticate）、跨仓库 mount、垃圾回收、`Accept` 内容协商。
