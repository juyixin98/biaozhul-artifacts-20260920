# repro-pack — 可重复制品打包服务

纯后端 HTTP 服务（Rust + Axum）：把服务端某个目录打包成**确定性**的 tar 归档。
相同内容无论何时、何地、以何种顺序输入，产出**字节完全一致**的包，并输出内容清单（manifest）与 SHA-256 摘要。

## 确定性规范（显式规则）

| 维度 | 规则 |
|---|---|
| 文件排序 | 所有条目按**规范化路径的字节序**（UTF-8 码点序）排列，与 `read_dir` 枚举顺序、文件创建顺序无关 |
| 时间戳 | 所有条目 mtime 固定为 **0**（Unix epoch），不读取文件实际 mtime，与宿主时区无关 |
| 权限映射 | 文件：任一执行位 → `0755`，否则 `0644`；目录 `0755`；符号链接 `0777`。不保留宿主完整权限位 |
| 属主 | uid = gid = 0，uname/gname 为空 |
| 归档格式 | 纯 ustar（无 GNU/PAX 扩展头），超长路径用 prefix 字段拆分，无法拆分时报错 |
| 路径规范化 | `/` 分隔；忽略空组件与 `.`；**拒绝**空路径、绝对路径、`..` 组件、`\`、NUL、非 UTF-8 |
| 路径冲突 | 两条不同原始路径规范化为同一路径（如 `src//main.rs` 与 `src/./main.rs`）→ **拒绝**，HTTP 409 |
| 符号链接 | 记录为链接条目（不跟随）；目标必须是**相对路径**且解析后**不得逃逸根目录**，否则拒绝；目标会被规范化。拒绝绝对目标 |
| 特殊文件 | FIFO/套接字/设备文件一律拒绝 |

## 构建与启动

依赖：Rust 工具链（≥ 1.75，开发使用 1.98.1）。其余依赖见 `Cargo.toml`，版本由 `Cargo.lock` 锁定。

```bash
cargo build --release
PORT=8080 ./target/release/repro-pack
# 输出: repro-pack listening on http://0.0.0.0:8080
```

## HTTP 接口

### `GET /health`
返回 `200 ok`。

### `POST /pack`

请求体（JSON）：

```json
{
  "root": "/path/to/dir",
  "paths": ["src/main.rs", "README.md"]   // 可选；缺省扫描整个目录
}
```

响应由 `Accept` 头决定：

- `Accept: application/json`（默认）→ 内容清单 JSON，含每个条目的路径/类型/权限/大小/SHA-256，以及整个归档的 SHA-256 与字节数；
- `Accept: application/x-tar` → tar 字节流，响应头带 `X-Archive-Sha256`、`X-Archive-Size`。

错误码：`404` 目录不存在；`400` 非法路径/路径穿越/特殊文件；`409` 冲突规范路径；`500` 其他。

### 请求样例

```bash
# 1. 获取清单与摘要
curl -s -X POST http://127.0.0.1:8080/pack \
  -H 'content-type: application/json' \
  -d '{"root": "/path/to/dir"}' | jq .

# 2. 下载 tar 包
curl -s -X POST http://127.0.0.1:8080/pack \
  -H 'content-type: application/json' \
  -H 'accept: application/x-tar' \
  -d '{"root": "/path/to/dir"}' -o artifact.tar

# 3. 只打包指定文件
curl -s -X POST http://127.0.0.1:8080/pack \
  -H 'content-type: application/json' \
  -d '{"root": "/path/to/dir", "paths": ["README.md", "src/main.rs"]}'
```

清单响应示例（节选）：

```json
{
  "format": "repro-pack/1",
  "entries": [
    {"path": "README.md", "kind": "file", "mode": "0644", "size": 25,
     "sha256": "…"},
    {"path": "src", "kind": "dir", "mode": "0755"},
    {"path": "src/main.rs", "kind": "file", "mode": "0755", "size": 13,
     "sha256": "…"}
  ],
  "archive": {"sha256": "03520a8b…", "size_bytes": 10240}
}
```

## 测试与验收

```bash
cargo test                # 16 个自动化测试（单元 + 集成 + HTTP 端到端）
./scripts/acceptance.sh   # 验收脚本：真实启动服务验证确定性
```

验收脚本覆盖验收标准：

1. **枚举顺序无关**：两份内容相同、创建顺序相反的目录 → 摘要一致；
2. **mtime 无关**：文件 mtime 分别设为 1999 与 2030 → 摘要一致；
3. **时区无关**：服务分别以 `TZ=America/New_York` 和 `TZ=Asia/Shanghai` 运行 → 摘要一致；
4. **冲突拒绝**：`["src//main.rs", "src/./main.rs"]` → HTTP 409；
5. **路径穿越拒绝**：`../../etc/passwd` → HTTP 400；
6. 产出的 tar 用系统 `tar -tvf` 校验可读、权限/时间戳符合规范。

### 实测结果（2026-09-24，Linux 6.8，rustc 1.98.1）

- `cargo test`：**16/16 通过**（6 单元 + 10 集成，含 HTTP 端到端）。
- `./scripts/acceptance.sh`：全部 PASS。两种时区、两种创建顺序、不同 mtime 下摘要均为
  `03520a8b6a41476dcafcb73579665b6a4002070077ebe22b8f98cfdc1c4c4d93`；
  冲突路径返回 `409 {"error":"conflicting normalized path: \"src/main.rs\""}`。

## 已知限制 / 未完成项

- 打包在内存中完成（读全部文件内容后一次性写出），不适合超大目录；后续可改为两遍扫描（先清单后流式写出）。
- 输入为服务端文件系统路径，未做鉴权与根目录白名单限制；部署在不可信环境时需自行加访问控制。
- 超长符号链接目标（>100 字节）受 ustar 限制会报错，未实现 PAX 扩展头兜底。
- 时区无关性通过「代码不读取任何时钟/时区」从机制上保证，并以两种 `TZ` 实测验证；未做更多时区的穷举。
- 仅支持 Unix 语义（权限位、符号链接）；非 Unix 平台权限映射退化为固定值。
