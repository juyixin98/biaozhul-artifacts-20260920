# OCI 分层白名单解包（layered whitelist unpack）

纯后端的**离线 OCI 镜像层重建**服务：只处理本地 fixture 镜像，按层应用文件与
whiteout、处理不透明目录，严格拒绝路径穿越、越界符号链接、设备文件与过量解压；
**所有层摘要（SHA-256）校验通过后才参与重建，任何一步失败都不会发布部分根目录**；
镜像不会被运行。重建成功后输出每个最终路径的**来源层**、内容摘要与整棵根文件系统
的确定性摘要。

技术栈：Rust + [Axum](https://github.com/tokio-rs/axum) 0.8 + [tar](https://crates.io/crates/tar)
+ flate2（gzip）+ sha2（真实 SHA-256，无占位/模拟加密）。

---

## 目录结构

```
src/
  main.rs        HTTP 服务入口（CLI、优雅关闭）
  server.rs      Axum 路由与 JSON 协议
  rebuild.rs     重建编排：暂存 → 校验 → 应用 → 根摘要 → 原子发布；来源报告
  extractor.rs   逐层两趟应用：whiteout 趟 + 白名单内容趟；配额记账
  pathsafe.rs    路径净化与符号链接/硬链接越界的词法闭包判定
  oci.rs         只读 OCI image layout（oci-layout / index / manifest / config）
  digest.rs      OCI sha256 摘要：解析、真实哈希、常量时间比较、流式哈希
  limits.rs      过量解压配额（解压炸弹 / 条目数 / 文件数 / 路径长度）
  error.rs       统一错误类型与 HTTP 状态映射
examples/
  make_fixtures.rs  生成 5 个示例本地镜像（含 4 个恶意镜像）
tests/           27 个自动化测试（单元 + 解包行为 + HTTP API）
workdir/         运行时目录（images/ 输入、staging/ 暂存、roots/ 发布产物）
```

## 安全模型（白名单）

| 威胁 | 处理 |
|---|---|
| 路径穿越 `../x`、绝对路径 `/x`、盘符/反斜杠 | `sanitize_relative` 逐条净化，拒绝 |
| 越界符号链接（绝对目标 / 多余 `..` / 链接链逃逸） | 对每个 symlink 做**词法闭包**判定：按链接所在目录逐级规范化目标，一旦 `..` 下溢根目录即拒绝；由此归纳保证任何内核路径解析都无法离开根目录 |
| 沿既有符号链接写入（如 `s` 是链接，下一层写 `s/leaf`） | 落盘全程用 `symlink_metadata`，绝不跟随链接；挡路的链接先删除再建成真实目录 |
| 字符/块设备、FIFO、socket、GNU/PAX sparse | 条目类型白名单：仅**目录、普通文件（含硬链接）、安全符号链接**，其余拒绝（连 `GNU.sparse.*` PAX 扩展伪装也拒绝） |
| 解压炸弹 / 过量解压 | gzip 解压阶段和 tar 两趟应用都有流式上限：单层解压字节数、总解压字节数、单文件大小、条目数、文件数、路径长度、存储 blob 大小 |
| 篡改层（摘要不符） | 重建前先对**全部**层做 SHA-256 校验，任一不符直接失败，不应用任何一层 |
| 损坏 gzip / 截断 | 摘要通过后全量解压一遍以校验 gzip CRC/ISIZE；tar 解析错误归类为损坏 |
| setuid/setgid | 提取时强制剥离（保留普通权限位与 sticky） |
| 部分根目录泄露 | 重建在唯一 `staging/` 目录内完成；**全部成功**后 `rename` 原子发布到 `roots/<img>/<rootfs摘要>/` 并原子翻转 `latest`；失败即删除暂存目录，旧版本不受影响 |

> 注意：层摘要覆盖的是**存储字节**（gzip 压缩后的 blob），这与 OCI 规范一致；
> gzip 完整性在摘要通过后单独验证，因此「摘要被伪造式重算但压缩流损坏」与
> 「blob 被篡改导致摘要不符」两种情形都能被准确区分并如实报错。

## 重建语义

- 层严格按 manifest 顺序应用。
- `.wh.<name>`：删除同目录下的 `<name>`（文件或目录子树），whiteout 标记自身不落盘。
  同一层里「先删再建」生效：whiteout 趟先删，内容趟再建新内容。
- `.wh..wh..opq`：不透明目录，应用本层内容前清空该目录**已有**的全部子项。
- 同一路径被多层覆盖时，以最后一次物化的层为准（文件/目录/符号链接类型可被后续层改变）。
- 硬链接目标必须是已物化的普通文件（本层或更早层），且目标路径同样不得越界。

## 输出

- `rootfsDigest`：对最终根文件系统的确定性 Merkle 式 SHA-256（目录=排序后的子项记录；
  文件=内容；符号链接=目标字符串；混入节点类型与权限位）。与层应用顺序、目录遍历顺序
  无关，只描述最终树；同一镜像重复重建摘要稳定。
- `files[]`：每个最终路径的 `kind`、`sourceLayer`（来源层摘要）、`layerIndex`、
  `contentSha256`（文件）、`symlinkTarget`（链接）、`mode`。

---

## 本地启动

需要 Rust 工具链（开发环境为 rustc/cargo 1.98）。依赖已在 `Cargo.lock` 锁定，
可离线构建（本机已验证）：

```bash
cargo build --release --offline        # 或 cargo build --release（联网）
```

生成示例镜像：

```bash
cargo run --release --example make_fixtures -- ./workdir
```

启动服务（默认 127.0.0.1:8080，可用参数或环境变量覆盖）：

```bash
./target/release/oci-unpack-server --workdir ./workdir --port 8080
# 等价环境变量：OCI_ADDRESS / OCI_PORT / OCI_WORKDIR
```

## HTTP 协议（纯 JSON）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET  | `/health` | 存活检查 |
| GET  | `/images` | 列出 `workdir/images` 下的本地镜像 |
| POST | `/rebuild/{image}` | 校验并重建；成功才发布，返回完整报告 |
| GET  | `/rebuild/{image}` | 读取最近一次成功重建的持久化报告 |
| GET  | `/rebuild/{image}/file?path=app/version` | 查询单个最终路径的来源层 |
| GET  | `/rebuild/{image}/layers` | 有序来源层摘要与根摘要 |

状态码：成功 `200`；镜像/报告不存在 `404`；非法镜像名 `400`；
层摘要不符、归档损坏、越界/设备/超限等 `422`（错误体含具体原因）；内部错误 `500`。

## 验收命令

一键脚本（生成夹具 → 构建 → 启动 → 正向与恶意用例 curl 断言 → 关闭）：

```bash
./scripts/acceptance.sh
```

手动示例（服务已启动时）：

```bash
# 正向：三层镜像，删除再创建 + 不透明目录 + 多层覆盖
curl -s -X POST http://127.0.0.1:8080/rebuild/demo-app | python3 -m json.tool

# 路径来源
curl -s 'http://127.0.0.1:8080/rebuild/demo-app/file?path=app/version'

# 恶意：均应 HTTP 422 且 workdir/roots/<name>/ 不存在
for img in evil-traversal evil-symlink evil-device corrupt-layer; do
  curl -s -w '\n[%{http_code}]\n' -X POST http://127.0.0.1:8080/rebuild/$img
done
```

发布产物可直接检查：

```bash
ROOT=workdir/roots/demo-app/$(cat workdir/roots/demo-app/latest)
find "$ROOT" -mindepth 1 | sort
cat "$ROOT/app/version"        # 3.0（第 3 层删除并重建后的内容）
```

示例镜像说明：

| 镜像 | 覆盖点 |
|---|---|
| `demo-app` | 3 层：基础层 → 同路径多层覆盖 → 删除再创建 + 不透明目录清空 `app/logs` |
| `evil-traversal` | `../escape.txt` 父目录穿越 |
| `evil-symlink` | 指向 `/bin/sh` 的越界绝对符号链接 |
| `evil-device` | 字符设备节点（major 1/minor 3） |
| `corrupt-layer` | 摘要地址正确但 gzip 压缩流被位翻转损坏 |

## 自动化测试

```bash
cargo test --offline        # 27 个测试
```

覆盖：多层覆盖与来源归属、删除再创建（同层/跨层）、父目录遮蔽（opaque）、
路径穿越、越界符号链接（绝对/多余 `..`/链接链）、设备/FIFO 拒绝、硬链接越界与
正常共享、损坏 gzip、blob 篡改摘要不符且不发布、解压炸弹（plain 与 gzip）、
条目数超限、失败重建保留旧版本、setuid 剥离、后续层不沿符号链接写入，以及 HTTP
层 200/404/422 协议。

## 边界与说明

- 仅支持 `sha256` 摘要（OCI 强制算法）与 tar / tar+gzip 层媒体类型；OCI 与
  Docker v2 的清单/层媒体类型均接受。
- 只做解包与重建，不执行镜像、不解析/运行任何文件内容。
- 服务按本地单用户 fixture 后端设计，重建串行化（避免同镜像并发发布竞争）。
