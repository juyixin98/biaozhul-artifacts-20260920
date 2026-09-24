# OCI 分层白名单解包（离线镜像层重建后端）

纯后端（Rust + Axum + `tar`），用于重建**本地离线** OCI 镜像 fixture：
按层应用文件变更与 OCI whiteout、正确处理 opaque（不透明）目录；拒绝路径
穿越、越界符号链接、设备文件和过量解压（zip-bomb）。**每层摘要真实校验
通过后才参与重建，任一层失败则不发布任何部分根目录；不运行镜像。**

输出每个最终文件的**来源层**与镜像的**最终聚合摘要**。

---

## 1. 它做什么

对一个本地 OCI image-layout 目录：

```
<fixtures>/<name>/
  oci-layout                 {"imageLayoutVersion":"1.0.0"}
  index.json                 OCI image index
  blobs/sha256/<hex>         内容寻址 blob（manifest/config/layer）
```

执行：

1. **加载与结构校验**：`oci-layout`、`index.json`、manifest、config 全部解析；
   manifest、config 的摘要用真实 SHA-256 复核。
2. **逐层两遍流式管线**（每层独立两遍，不在内存缓存整个层）：
   - **verify 遍**：对压缩 blob 字节做 SHA-256；gzip 解压；逐 tar 头校验路径/
     链接目标；把每个文件 payload 流过限额计数器；对**解压后**的 tar 做
     SHA-256（OCI `rootfs.diff_ids`）。摘要不符即中止。
   - **apply 遍**：重放同一流，把条目物化到**暂存目录**，再次哈希并断言摘要。
3. **whiteout / opaque 语义**：`.wh.<name>` 隐藏下层同名（含目录子树），
   `.wh..wh..opq` 隐藏该目录下所有下层内容。标记只影响**更低层**，同层已创建
   的内容不受影响（与 tar 内成员顺序无关）。
4. **原子发布**：全部层成功后，`rename` 暂存目录到内容寻址的 build 目录，再原子
   替换 `latest` 符号链接。**失败时丢弃暂存目录，绝不发布半成品根。**
5. 计算**最终聚合摘要**（对“镜像标识 + config/manifest 摘要 + 各层摘要/尺寸 +
   排序后的最终文件来源清单”的规范序列化做真实 SHA-256），可复现。

### 安全策略（拒绝项）

| 威胁 | 处理 |
|---|---|
| 路径穿越（`../`、绝对路径、反斜杠/盘符、NUL） | `path_traversal` 拒绝，路径经词法规范化，落盘路径仅由 push 组件拼出 |
| 越界符号链接（绝对目标、`..` 逃出根） | `link_escape` 拒绝；目标按链接父目录做词法解析，深度计数器归零即逃 |
| 经符号链接目录穿越写入（跨层） | 任何条目的祖先若是符号链接则 `ancestor_symlink` 拒绝 |
| 设备/字符/FIFO/socket 等 | `unsafe_entry_type` 拒绝；并剥离 setuid/setgid 位 |
| 过量解压 | 压缩大小、解压大小、单文件大小、总 payload、条目数、硬链接数全部限额 |
| 损坏压缩包 | 截断 gzip→`gzip_error`；解压成功但非 tar→`tar_error`（如实区分） |
| 摘要不符（压缩 blob 或解压 diff_id） | `digest_mismatch`，附期望/实际两个摘要 |
| 悬空/指向目录的硬链接 | `conflict` 拒绝 |

> 不执行任何镜像内容：无 `chroot`、无 exec、无挂载。仅做文件物化。

---

## 2. 工作区结构

```
Cargo.toml              workspace
oci-unpack/             核心库 + Axum 服务
  src/
    main.rs             HTTP 服务二进制
    server.rs           路由与处理器（可被集成测试直接驱动）
    rebuild.rs          编排：暂存→逐层验证/应用→原子发布→最终摘要
    layer.rs            两遍管线、whiteout/opaque、来源追踪
    digest.rs           真实 SHA-256、OCI 描述符 sha256:<hex> 解析
    path.rs             路径/符号链接词法安全分析
    limits.rs           解压与条目限额
    io_util.rs          哈希/计量 reader、gzip、EOF 排空
    fsops.rs            原子写、lstat 安全的替换/链接原语
    model.rs            最小 OCI image-layout 模型
    error.rs            错误类型与 HTTP 状态/错误码映射
  tests/                37 个自动化测试（语义/限额/路径/HTTP）
fixture-maker/          生成正常与恶意 OCI fixture（库 + 二进制）
fixtures/               已生成的示例输入（13 个镜像）
```

锁定依赖见根目录 `Cargo.lock`（构建时自动生成，已提交）。

---

## 3. 本地构建与启动

需要 Rust 工具链（在 1.98 上验证）。

```bash
# 1) 构建（debug 或 release）
cargo build --release

# 2) 生成示例 fixture（默认输出到 ./fixtures）
./target/release/fixture-maker fixtures

# 3) 启动服务
OCI_FIXTURES_DIR=fixtures \
OCI_BUILDS_DIR=builds \
OCI_BIND=127.0.0.1:8080 \
./target/release/oci-unpack-server
```

环境变量（均有默认值）：

| 变量 | 默认 | 含义 |
|---|---|---|
| `OCI_FIXTURES_DIR` | `fixtures` | 本地镜像根目录 |
| `OCI_BUILDS_DIR` | `builds` | 发布产物根目录 |
| `OCI_BIND` | `0.0.0.0:8080` | 监听地址 |
| `OCI_MAX_COMPRESSED` / `OCI_MAX_DECOMPRESSED` / `OCI_MAX_ENTRIES` | 见 `limits.rs` | 覆盖限额（字节/条数） |

### HTTP 协议（JSON）

| 方法与路径 | 说明 |
|---|---|
| `GET  /healthz` | 健康检查 |
| `GET  /v1/images` | 列出本地 fixture |
| `POST /v1/images/{name}/rebuild` | 校验 + 重建 + 原子发布 |
| `GET  /v1/images/{name}/builds` | 列出某镜像已发布 build 与 `latest` |

成功 `200`，fixture 不存在 `404`，名字非法 `400`，**镜像被拒绝 `422`**，错误体形如：

```json
{ "error": { "code": "link_escape",
             "message": "symlink target escapes root: link=\"a/b/link\" target=\"../../../../escape\"" } }
```

成功响应含：`final_digest`、各层 `digest`/`diff` 尺寸统计/whiteout 计数，以及
`files[]`（`path`/`kind`/`layer_index`/`layer_digest`，即**文件来源层**）。

---

## 4. 一键验收

```bash
# 自动化测试（37 个：层语义 / 限额 / 路径安全 / HTTP API）
cargo test

# 端到端验收脚本：起服务、打正常镜像、打全部恶意镜像、断言状态码与“不发布”
./scripts/acceptance.sh
```

手动验收示例：

```bash
curl -s -X POST http://127.0.0.1:8080/v1/images/delete-recreate/rebuild | jq .
curl -s -X POST http://127.0.0.1:8080/v1/images/link-escape/rebuild | jq .   # 422
# 查看重建出的真实文件
cat builds/delete-recreate/latest/rootfs/app/version.txt   # layer2-final
```

---

## 5. 示例输入（fixtures）

`fixture-maker` 生成 13 个镜像：

**正常（应 200）**
- `delete-recreate` — 删除后重建 + 同一路径多层覆盖（3 层）
- `opaque-shadow` — 父目录 opaque 遮蔽下层内容
- `links-and-hardlinks` — 合法硬链接（同 inode）与收敛符号链接
- `plain-tar` — 未压缩 tar 层

**恶意/异常（应 422，且不发布）**
- `link-escape`、`link-absolute` — 相对 `..` 逃逸 / 绝对符号链接
- `path-traversal`、`absolute-path` — `../../` 与绝对路径成员（手工构造 ustar 头）
- `device-file` — 字符设备
- `corrupt-gzip` — 第二层 gzip 被截断（第一层有效也必须整体不发布）
- `garbage-gzip` — gzip 解压成功但内容不是 tar
- `digest-tampered` — manifest 摘要造假（blob 真实摘要不符）
- `diffid-tampered` — blob 合法但 config 的**解压后** diff_id 造假

---

## 6. 任务点名场景的对应测试

| 场景 | 测试 |
|---|---|
| 删除再创建 | `delete_then_recreate` |
| 父目录遮蔽 | `opaque_directory_shadows_parent_content`、`whiteout_removes_directory_subtree` |
| 链接逃逸 | `rejects_escaping_symlink_deep`、`rejects_absolute_symlink`、`rejects_cross_layer_write_through_symlink_ancestor`、`rejects_write_beneath_contained_symlink_to_avoid_host_write` |
| 损坏压缩包 | `rejects_corrupt_truncated_gzip`、`rejects_non_tar_gzip_garbage` |
| 同一路径多层覆盖 | `same_path_overridden_across_layers`、`directory_replaced_by_file_and_vice_versa` |
| 摘要造假（压缩/解压） | `rejects_digest_tampering`、`rejects_uncompressed_diff_id_tampering` |
| 过量解压 | `rejects_decompression_bomb_via_payload_limit`、`rejects_too_many_entries`、`rejects_compressed_size_over_cap` |
| 失败不发布 | 每个拒绝测试都断言 `!published()`，另见 `later_good_build_is_not_blocked_by_prior_failure` |

---

## 7. 设计说明与边界

- **两遍而非一遍**：apply 遍开始前，该层已完整验证；且 apply 遍再次哈希，
  防止 TOCTOU 式的 blob 在两遍之间被替换（两遍读取同一文件路径并各自断言）。
- **整个重建先写暂存、成功才发布**：因此即便第 N 层失败，前 N-1 层的产物也只
  存在于被删除的暂存目录中，`latest` 永远指向一套完整可用的根。
- **符号链接策略偏保守**：收敛的悬空/指向目录内的链接本身允许；但**禁止在符号
  链接祖先路径下新建文件**，从根本上杜绝“经链接写到宿主”。
- 最终聚合摘要是本工具定义的、覆盖“层历史 + 最终来源清单”的确定性摘要（不是
  OCI 规范字段）；每层 blob/diff_id 摘要则严格遵循 OCI 内容校验语义。
- 平台：Linux（依赖 Unix 符号链接/权限/设备类型语义与 `st_ino`）。
