# build-output-merge

构建动作输出树合并服务（纯后端，Rust + Axum）。

多个“构建动作”各自声明一棵输出树（文件 / 目录 / 符号链接），服务把它们合并成
一棵目标树。**合并前先形成完整计划**，发现任何冲突都直接拒绝、目标树不做任何
改动；只有计划无冲突时才一次性原子落盘。

检测三类问题：

1. **文件 / 目录冲突**：同路径类型不一致（文件 vs 目录），或某文件/符号链接是
   另一条输出的祖先（经典的文件 `a` 与目录 `a/b`）。
2. **大小写折叠碰撞**：在大小写不敏感文件系统（macOS HFS+/APFS 默认、Windows
   NTFS）上会指向同一个名字的路径，如 `Readme` 与 `README`；也覆盖仅折叠后才
   成为祖先的情况。
3. **符号链接目标规范化**：词法归一化（折叠 `.`、重复斜杠、解析 `..`），
   拒绝绝对目标和会越出输出根目录的相对目标（`../../etc/passwd` 之类）。

同时识别**同内容共享**：字节一致（SHA-256 相同）的多个文件在计划中归为一组，
落盘时只写一份 blob 并硬链接到各路径。

---

## 1. 依赖与启动

### 运行依赖

- Rust / Cargo（开发环境：`rustc 1.98.1`、`cargo 1.98.1`；edition 2021，
  更早的稳定版大概率可用，未逐一验证）。
- 类 Unix 系统（落盘使用 `symlink(2)`、`link(2)`；硬链接失败会自动回退为拷贝，
  符号链接仍需平台支持）。
- 构建时需要访问 crates.io 索引与下载源。仓库 `~/.cargo/config.toml` 已配清华
  镜像；无镜像环境用官方源即可。

### 主要库依赖（已锁定，见 `Cargo.lock`，共 81 个传递依赖）

| crate | 用途 |
|---|---|
| axum 0.8 / tokio 1 | HTTP 服务与异步运行时 |
| serde / serde_json | 请求与响应 JSON |
| sha2 / hex | 文件内容 SHA-256 |
| base64 | 二进制文件内容传输 |
| uuid | 自动生成合并 id |
| tempfile / tower 0.5（dev） | 集成测试 |

### 启动命令

```bash
cargo build --release                       # 或 cargo build（debug）
./target/release/build-output-merge \
    --listen 127.0.0.1:8080 \
    --base-dir var/merges
```

- `-l, --listen <ADDR>`：监听地址，默认 `127.0.0.1:8080`。
- `-d, --base-dir <DIR>`：合并结果发布目录，默认 `var/merges`；不存在会自动创建。
  每次成功合并生成 `<base-dir>/<merge_id>/`。
- Ctrl-C 优雅退出。

---

## 2. HTTP 接口

| 方法/路径 | 说明 | 成功 | 失败 |
|---|---|---|---|
| `GET /` | 服务与接口清单 | 200 | — |
| `GET /health` | 存活检查，回显 base dir | 200 | — |
| `POST /merge` | 规划并（非 dry-run 时）原子落盘 | 200 | 400 请求非法 / 409 有冲突或 id 被占 / 500 落盘错误 |
| `GET /merge/{merge_id}` | 查询已成功合并的记录 | 200 | 404 |

### 请求体

```jsonc
{
  "merge_id": "可选；不给则自动生成。[A-Za-z0-9_-]，1-128 字符，不得以点开头",
  "case_sensitive": false,   // 可选，默认 false：启用大小写折叠检测
  "dry_run": false,          // 可选，true 时只出计划、绝不写盘
  "actions": [
    {
      "id": "compile",       // 动作 id，请求内唯一、非空
      "outputs": [
        { "path": "bin/app", "kind": "file", "content_base64": "RUZM..." },
        { "path": "bin/app", "kind": "file", "content": "文本内容（二选一）" },
        { "path": "lib",     "kind": "dir" },
        { "path": "share/latest", "kind": "symlink", "link_target": "README.md" }
      ]
    }
  ]
}
```

约束：

- `path` 必须是相对 POSIX 路径：拒绝绝对路径、空路径、`.`/`..` 段、空段
  （重复或结尾斜杠）、NUL 字节。
- `file` 必须且只能给 `content`（UTF-8 文本）或 `content_base64`（标准 base64）。
- `dir` 不能带内容或链接目标；`symlink` 必须给 `link_target`，不能带文件内容。
- 请求体上限 16 MiB。

### 冲突响应（HTTP 409）关键字段

每个冲突给出：

- `kind`：冲突类型（见下）；
- `path`：**最短冲突路径**——能定位冲突的最浅路径（祖先冲突落在祖先节点，
  同深度大小写碰撞取路径较短者，等长取字典序最小者）；
- `other_paths`：涉及的其余路径（祖先冲突时是被阻塞的后代路径列表）；
- `actions`：**冲突来源动作**的去重 id 列表；
- `detail`：人类可读说明（同路径异内容时附两个 SHA-256）。

`kind` 取值：

| kind | 含义 |
|---|---|
| `same_path_incompatible` | 同路径类型冲突，或同为文件但内容不同 / 同为链接但目标不同 |
| `ancestor_blocking` | 文件或符号链接占据了后代路径所需的目录位（`a` vs `a/b`） |
| `case_fold_collision` | 不同路径折叠大小写后相同（同深度） |
| `case_fold_ancestor` | 祖先位仅在大小写折叠后与文件/链接冲突 |
| `unsafe_link_target` | 符号链接目标为绝对路径或经 `..` 越出输出根 |

响应同时回传完整 `plan`（每个节点的最终类型、来源动作+输出下标、内容哈希、
链接目标，以及 `shared_content` 共享分组），便于只做检查不落盘。

---

## 3. 请求样例

样例文件在 `examples/`，可一键复现：

```bash
./examples/run-examples.sh 18080
```

或手动：

```bash
# 正常合并（含同内容共享与安全链接）→ 200
curl -i -X POST http://127.0.0.1:8080/merge \
  -H 'Content-Type: application/json' \
  --data-binary @examples/01-happy-path.json

# 文件 a 与 a/b/config.txt 目录冲突 → 409
curl -i -X POST http://127.0.0.1:8080/merge \
  -H 'Content-Type: application/json' \
  --data-binary @examples/02-file-vs-dir.json

# 跨动作同路径异内容 → 409（附两个哈希）
curl -i -X POST http://127.0.0.1:8080/merge \
  -H 'Content-Type: application/json' \
  --data-binary @examples/03-same-path-different-content.json

# Readme / README 大小写折叠碰撞 → 409
curl -i -X POST http://127.0.0.1:8080/merge \
  -H 'Content-Type: application/json' \
  --data-binary @examples/04-case-fold.json

# 符号链接 ../../etc/passwd 越界 → 409
curl -i -X POST http://127.0.0.1:8080/merge \
  -H 'Content-Type: application/json' \
  --data-binary @examples/05-unsafe-link.json

# dry_run 只规划不写盘
curl -i -X POST http://127.0.0.1:8080/merge \
  -H 'Content-Type: application/json' \
  --data-binary @examples/06-dry-run-conflict.json

# 查询
curl -i http://127.0.0.1:8080/merge/demo-happy
```

### 期望结果（实际运行记录见第 5 节）

- `02` 响应：`kind=ancestor_blocking`、`path="a"`、
  `other_paths=["a/b/config.txt"]`、`actions=["action-a","action-b"]`。
- `03` 响应：`kind=same_path_incompatible`、`path="out/result.txt"`、
  `actions=["action-a","action-b"]`，`detail` 含两个不同 SHA-256。
- `01` 落盘后 `lib/libcore.a` 与 `lib/libcore.copy.a` 为同一 inode 的硬链接；
  `share/latest` 是指向 `README.md` 的相对符号链接。

---

## 4. 设计与语义边界

**先计划后落盘（失败不改变目标树）**

- `planner` 是纯函数：输入所有动作输出，输出完整 `Plan`（节点 + 冲突 + 共享组），
  不碰文件系统，可独立单测。
- 只有 `plan.conflicts.is_empty()` 时才调用 `apply`。
- `apply` 先在 base 目录内的**同级暂存目录** `.merge_staging.<id>/` 构建整棵树
  （保证与最终目录同一文件系统），内容写在 `.merge_blobs.<id>/`，最后一次
  `rename(2)` 原子发布为 `<id>/`。任何 I/O 错误都会清理暂存与 blob 目录；
  最终目录要么完整出现、要么不存在。已发布的历史合并从不被原地修改
  （每次合并发布到独立的新目录）。

**同路径多来源的归并规则**

- 文件：字节一致（SHA-256 相同）→ 合并为一个节点，记录全部来源动作；不同 →
  `same_path_incompatible`。
- 目录：任意数量同名目录自动兼容（父目录按需隐式存在）。
- 符号链接：目标字符串一致才兼容。
- 类型互不相同时（如文件 vs 目录）直接冲突。

**大小写折叠**

- 采用 Unicode 简单的 `to_lowercase()` 逐段折叠（对 ASCII 文件名与 Unix 常见
  文件名足够；与 NTFS/HFS+ 的逐字符折叠基本一致）。这不是完整 Unicode
  case folding（不处理 `ﬃ` 一类多字符展开），见“未完成项”。
- 请求可设 `"case_sensitive": true` 关闭折叠检测（目标确定为 ext4 等敏感
  文件系统时使用）。

**符号链接目标规范化**

- 纯词法分析，不触碰、不要求链接目标在树内真实存在（构建产物里允许悬空链接）。
- 判定标准：把相对目标相对“链接所在目录”解析，`..` 层数超过已命名祖先即越界；
  以 `/` 开头的绝对目标一律拒绝。归一化结果（折叠 `.`、`//` 后）写入冲突详情。

**其他边界**

- 路径只按 POSIX `/` 分隔解析；反斜杠视为普通文件名字符，不做 Windows 路径处理。
- 不落盘文件权限/属主/mtime：文件统一默认权限，目录默认权限，内容由请求给出。
- 合并记录保存在进程内存中（`GET /merge/{id}`）；重启后记录消失，但磁盘上的
  树仍在。无鉴权、无多副本协调——定位为本地/内网构建服务。

### 源码结构

```
src/path.rs     相对路径与链接目标的解析、归一化
src/model.rs    领域类型（输出、计划节点、冲突、共享组）
src/planner.rs  纯逻辑：六阶段冲突检测与计划生成
src/apply.rs    暂存 + blob/硬链接 + 原子 rename 落盘
src/api.rs      Axum 路由、请求校验、编排、查询
src/main.rs     命令行参数与服务启动
src/lib.rs      库入口（供集成测试复用）
tests/api.rs    14 个端到端 HTTP 测试
examples/       6 个请求样例 + 复现脚本
```

---

## 5. 测试与实际运行结果

以下为在本环境（Linux 6.8、Rust 1.98.1）的**实际运行记录**。

### 自动化测试

命令：`cargo test`

- 库单元测试：**7 passed / 0 failed**
  （路径合法性、链接逃逸深度判定、文件-目录祖先冲突、同路径异内容、同内容
  共享、大小写碰撞及开关）。
- 集成测试 `tests/api.rs`：**14 passed / 0 failed**
  （健康检查；正常落盘 + inode 相同验证硬链接；文件 vs 目录冲突且来源动作与
  最短路径正确；冲突后 base 目录为空证明未写盘；同路径异内容冲突；同路径同
  内容接受并共享；大小写碰撞、关闭折叠、折叠祖先；越界链接 409；安全相对链接
  落盘且 readlink 正确；dry_run 不写盘；非法路径/非法类型 400；显式 id 重复
  409；落盘后 GET 查询 200 / 未知 id 404）。
- 合计 **21 passed / 0 failed**。`cargo clippy --all-targets` 无警告。

### 真实 HTTP 运行

启动 `./target/debug/build-output-merge --listen 127.0.0.1:18080
--base-dir var/merges` 后逐个 POST `examples/0*.json`：

| 样例 | 实际状态码 | 实际结果摘要 |
|---|---|---|
| 01 happy path | **200** | `status=applied`，6 个输出，1 个共享组，落盘 4 文件/1 目录/1 链接 |
| 02 文件 vs 目录 | **409** | `ancestor_blocking`，`path=a`，`other_paths=["a/b/config.txt"]`，actions 两个 |
| 03 同路径异内容 | **409** | `same_path_incompatible`，`path=out/result.txt`，detail 附两个不同 SHA-256 |
| 04 大小写碰撞 | **409** | `case_fold_collision`，`path=README`，`other_paths=["Readme"]`（等长取字典序最小） |
| 05 越界链接 | **409** | `unsafe_link_target`，`path=dir/link`，归一化目标 `etc/passwd` |
| 06 dry-run 冲突 | **409** | dry_run 也先出冲突；不写盘 |

落盘抽查（01）：

```
$ find var/merges/demo-happy -printf '%y %p inode=%i'
f .../lib/libcore.a       inode=3153128
f .../lib/libcore.copy.a  inode=3153128   # 同内容 → 同 inode 硬链接，links=2
l .../share/latest -> README.md
$ cat .../bin/app        # content_base64 解码
EFL-binary
$ ls var/merges
demo-happy               # 仅成功合并落盘；4 个 409 均未产生任何目录
```

接口抽查：`GET /merge/demo-happy` → 200 记录；`GET /merge/missing` → 404；
非法路径 `../x` → 400；非法 `kind="socket"` → 400；成功 dry-run → 200 且
base 目录无新增。

---

## 6. 未完成项 / 已知限制

如实记录当前未覆盖或有意取舍的部分：

1. **非完整 Unicode case folding**：仅做逐段 `to_lowercase()`，不处理一个字符
   展开为多个字符的特殊情形（如 `ﬃ`），也未模拟 macOS 的 NFD 归一化。对常规
   ASCII/UTF-8 构建产物足够。
2. **不保留 Unix 元数据**：文件权限位、可执行位、属主、mtime、xattr 均不保留；
   无法表达“可执行文件”这一构建产物需求。
3. **记录仅存内存**：`GET /merge/{id}` 的索引重启即失（磁盘树保留）；没有接入
   数据库或扫描 base 目录重建索引。
4. **单进程并发模型**：显式 `merge_id` 的占用检查靠进程内互斥锁；多实例共享
   同一 base 目录时没有文件锁/分布式协调。
5. **目标为 POSIX**：路径按 `/` 解析，依赖 `symlink`/`hard_link` 系统调用，
   未支持 Windows；Windows 上 `cargo build` 也会因 `std::os::unix` 失败。
6. **内容随请求内联**：只支持 `content` / `content_base64`，没有大文件流式上传
   或按内容哈希分块上传（16 MiB 请求上限）。
7. **无鉴权 / TLS / 限流**：按本地或内网可信服务定位，未加认证层。
