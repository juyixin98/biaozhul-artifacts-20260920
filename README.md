# 构建输出路径冲突合并服务（build-output-merge）

纯后端 HTTP 服务：把多个**构建动作**（build action）声明的输出树合并到一个目标目录，
在动手写盘**之前**先形成完整的合并计划，并检测全部路径冲突：

1. **文件 / 目录冲突** —— 文件（或符号链接）与目录占用同一路径，例如文件 `a` 与目录 `a/b`；
2. **同路径异内容冲突** —— 两个动作在同一路径产出不同内容（或不同符号链接目标，或文件↔符号链接混用）；
3. **大小写折叠碰撞** —— 同目录下仅大小写不同的名字（`Bin/Tool` 与 `bin/tool`），按 Unicode
   默认大小写折叠（Unicode Default Case Folding，使用 [`caseless`] crate，不止 ASCII）判定；
4. **符号链接目标规范化** —— 拒绝绝对路径，词法规范化 `.`/`..`，拒绝解析后逃逸出输出根的目标；
5. **非法路径** —— 绝对路径、空段、`.`/`..`、NUL、反斜杠、Windows 盘符、非法 base64 内容。

同一路径产出**相同内容**的多个动作不冲突：计划中合并为一个条目并记录全部来源动作；
落盘时内容按 sha256 去重（内容寻址对象池 + 硬链接，硬链接不可用时回退复制）。

**关键保证**：合并前先产出完整计划；只要存在冲突或构建暂存树失败，目标目录树**完全不变**。

## 技术栈与依赖

- Rust（开发/验证版本 `rustc 1.98.1`，edition 2024）
- Web 框架：[axum](https://crates.io/crates/axum) 0.8 + [tokio](https://crates.io/crates/tokio) 1
- 序列化：serde / serde_json
- 哈希：sha2（sha256）；base64（文件内容编码）
- Unicode 大小写折叠：[caseless](https://crates.io/crates/caseless) 0.2
- 稳定有序 map：indexmap（serde 特性）

版本锁定在 `Cargo.lock`。无需外部数据库、容器或系统服务；仅需一个 Rust 工具链即可 `cargo run`。
落盘使用了 Unix `symlink`，因此 `/apply` 只在 Unix（Linux/macOS）上支持；`/plan` 与平台无关。

## 目录结构

```
src/
  main.rs    进程入口（BIND_ADDR 环境变量配置监听地址）
  lib.rs     模块导出（供集成测试引用）
  http.rs    Axum 路由与处理函数
  model.rs   请求 / 响应数据模型
  paths.rs   路径校验、大小写折叠、符号链接目标规范化
  plan.rs    合并规划：虚拟树（trie）、全部冲突检测、计划生成
  apply.rs   暂存树构建 + 原子换名落盘
tests/api.rs 端到端 HTTP 测试（20 个测试用例，含真实落盘校验）
examples/    curl 用的请求样例 JSON
```

## 启动

```bash
# 调试运行
cargo run

# 或 release
cargo run --release

# 自定义监听地址（默认 0.0.0.0:8080）
BIND_ADDR=127.0.0.1:9000 cargo run --release
```

启动后健康检查：

```bash
curl -s http://127.0.0.1:8080/healthz
# {"service":"build-output-merge","status":"ok"}
```

## HTTP 接口

| 方法 & 路径   | 作用 |
|---|---|
| `GET  /healthz` | 健康检查 |
| `POST /plan`    | **只规划，不碰磁盘**。HTTP 恒为 200（除非请求本身 400），冲突在响应体 `ok=false` + `plan.conflicts` 中给出 |
| `POST /apply`   | 先规划；零冲突才原子落盘。冲突 → `409`（响应体带计划，目标树不变）；IO 错误 → `500` |

### 请求体

```json
{
  "target_dir": "/tmp/build-output/merged",
  "actions": [
    {
      "id": "compile-x86_64",
      "outputs": [
        { "path": "bin",        "type": "dir" },
        { "path": "bin/app",    "type": "file", "content_base64": "RUxG..." },
        { "path": "bin/latest", "type": "symlink", "target": "./app" }
      ]
    }
  ]
}
```

- `target_dir`：目标输出根目录（相对或绝对路径均可；其父目录会被自动创建）。
- `actions[].id`：动作标识，非空且请求内唯一；冲突报告的 `actions` 字段用它指明来源。
- 输出条目 `type`：`file` / `dir` / `symlink`。
  - `file` 需提供 `content_base64`（标准 base64；缺省视为空内容）。
  - `symlink` 需提供 `target`（相对路径）。
  - 路径一律 POSIX 风格相对路径。

### 冲突对象

```json
{
  "kind": "file_dir_conflict",      // 见下表
  "path": "a",                      // 最短冲突路径（冲突按路径深度升序，首个即最短）
  "actions": ["action-one", "action-two"],
  "detail": "action-two: path a must be a directory for a/b but it is a file/symlink"
}
```

| `kind` | 含义 |
|---|---|
| `file_dir_conflict`   | 文件/符号链接 与 目录互斥（含「文件下方还有子条目」） |
| `content_conflict`    | 同路径文件内容不同 / 符号链接目标不同 / 文件与符号链接互混 |
| `case_fold_collision` | 同目录大小写折叠后重名 |
| `symlink_escape`      | 链接目标为绝对路径，或规范化后逃逸出输出根，或缺少 target |
| `invalid_path`        | 输出路径非法，或文件内容不是合法 base64 |

### 状态码

- `200` 成功（`/plan` 有冲突也返回 200，看 `ok`；`/apply` 成功才 200）
- `400` 请求体无法解析 / `actions` 为空 / 动作 id 为空或重复
- `409` `/apply` 计划存在冲突（**不写任何东西**）
- `404` 未知路由；`500` 落盘 IO 错误

## 请求样例（curl）

```bash
# 1) 文件 a 与目录 a/b 的冲突 —— 最短冲突路径 a，来源两个动作
curl -s -X POST http://127.0.0.1:8080/plan \
  -H 'content-type: application/json' \
  -d @examples/conflict_filedir.json

# 2) 跨动作同路径异内容冲突
curl -s -X POST http://127.0.0.1:8080/plan \
  -H 'content-type: application/json' \
  -d @examples/conflict_content.json

# 3) 无冲突时落盘（同内容自动共享、符号链接目标规范化）
curl -s -X POST http://127.0.0.1:8080/apply \
  -H 'content-type: application/json' \
  -d @examples/merge_ok.json

# 4) 有冲突的 apply：HTTP 409，目标树原样保留
curl -s -i -X POST http://127.0.0.1:8080/apply \
  -H 'content-type: application/json' \
  -d @examples/conflict_filedir.json
```

内联最小样例（文件 `a` vs 目录 `a/b`）：

```bash
curl -s -X POST http://127.0.0.1:8080/plan \
  -H 'content-type: application/json' \
  -d '{
    "target_dir":"/tmp/out",
    "actions":[
      {"id":"A","outputs":[{"path":"a","type":"file","content_base64":"eA=="}]},
      {"id":"B","outputs":[{"path":"a/b","type":"file","content_base64":"eQ=="}]}
    ]}'
```

## 落盘原子性说明

`/apply` 在目标目录的**同一父目录**下创建两个临时目录：

- `.NAME.build-merge.stage-<pid>-<ns>`：完整的新输出树（目录 → 对象池硬链接文件 → 符号链接）；
- `.NAME.build-merge.obj-<pid>-<ns>`：按 sha256 分桶的内容对象池，构建成功后删除。

只有整棵暂存树构建成功，才执行改名：目标不存在则单次 `rename` 换入；目标已存在则先改名为
`.…backup-…`、再把暂存树改名为目标（第二步失败会把备份改回去）。任何失败都清理临时目录，
目标树恢复/保持调用前状态。

**已知限制（如实记录）**：目标已存在时的「备份 → 换入」是两次 `rename`，两次调用之间若进程被
SIGKILL 或机器断电，存在一个目标路径缺失的极小窗口；单步原子交换需要 Linux `renameat2`
的 `RENAME_EXCHANGE`，当前未使用。普通的「构建失败 / 冲突拒绝」路径不触发该窗口。

## 测试

```bash
cargo test
```

测试内容（`tests/api.rs`，通过 axum `oneshot` 直接打 HTTP，无需起端口；落盘用例真实写临时目录）：

- 文件 `a` 与 `a/b` 同动作 / 跨动作冲突，校验**最短冲突路径**与**来源动作列表**；
- 跨动作同路径异内容 → `content_conflict`；同路径同内容 → 共享、不冲突、sha256 正确；
- 不同路径相同内容 → 落盘为两个路径、一个内容对象，inode 相同（硬链接共享）；
- 大小写折叠碰撞（含检测来源动作）；
- 符号链接：`x/./y` 与 `x/y` 规范化等价后共享；绝对路径 / 越界 `..` 拒绝；合法 `../..` 保留；
- `/apply` 冲突时 409 且目标目录内容、父目录临时文件均无变化；成功后树内容与符号链接正确、
  再次 apply 整体替换旧树；
- 空 actions / 重复 id → 400；健康检查与 404。

## 未完成项 / 范围外

- 未实现「仅应用增量差」：`/apply` 每次整体构建并替换目标树（原子性更简单可靠）。
- 目标已存在时未使用 `renameat2(RENAME_EXCHANGE)` 单步原子交换（见上文已知限制）。
- 无鉴权 / TLS / 限流；纯本地后端服务，按需求未做任何前端界面。
- 文件内容经 base64 内联传输，适合中小体积构建产物；超大产物的流式传输未支持。
- Windows 上 `/apply` 不支持符号链接条目（`/plan` 仍可用于跨平台冲突预检）。
