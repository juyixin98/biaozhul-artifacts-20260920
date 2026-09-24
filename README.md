# repro-pack —— 确定性（可重复）制品打包服务

纯后端，无界面。用 Rust + [Axum](https://github.com/tokio-rs/axum) 实现：把一个
目录打成**字节可复现**的 GNU tar 包，并输出内容清单（manifest）与摘要（SHA-256）。

同一份**内容**，无论：

- 操作系统枚举目录（`readdir`）的顺序如何变化；
- 宿主机处于什么时区、locale；
- 文件 / 目录的 mtime、atime、uid/gid、宿主特有权限位是什么；
- 源目录位于磁盘上哪条绝对路径；

产出的 `.tar` 字节都完全一致（SHA-256 相同）。冲突的规范路径（路径穿越、符号链接
逃逸 / 指向目录 / 成环、硬链接别名）一律拒绝。

---

## 1. 目录结构与构建

```
Cargo.toml          依赖清单（随仓库提交 Cargo.lock，锁定依赖）
src/lib.rs          库入口
src/pack.rs         核心：遍历、规范化、权限映射、确定性 tar 写出
src/verify.rs       校验：摘要 / 清单 / 从源目录重建比对
src/server.rs       Axum HTTP 层（/health /pack /verify）
src/main.rs         CLI：serve / pack / verify
tests/              27 个自动化测试（库规则 + HTTP 集成 + 时区/mtime 端到端）
examples/requests.http   可直接在 REST Client / IDE 里跑的请求样例
```

### 依赖（运行时）

- Rust toolchain（开发环境实测 `rustc/cargo 1.98.1`，edition 2021）
- Linux（主要目标平台；用到 dev/inode、POSIX 权限位）
- 仅需标准构建工具（`cc` 不需要——依赖都是纯 Rust）

### Rust crate 依赖（见 `Cargo.lock` 锁定版本）

| crate       | 用途                       |
|-------------|----------------------------|
| axum 0.8    | HTTP 路由与处理            |
| tokio 1     | 异步运行时                 |
| tar 0.4     | tar 头/块编码（字段完全由本程序显式设置） |
| sha2 0.10   | SHA-256                    |
| hex 0.4     | 十六进制编码               |
| serde 1 / serde_json 1 | 清单与 API 的 JSON |
| clap 4      | CLI 参数解析               |

测试额外使用 `tempfile`、`tower`（`oneshot` 驱动路由器）、`http-body-util`。

### 构建

```bash
cargo build --release
# 产物：target/release/repro-pack
```

---

## 2. 启动命令

### 方式 A：HTTP 服务（默认 127.0.0.1:8080）

```bash
cargo run --release -- serve --bind 127.0.0.1:8080
# 或直接运行二进制：
target/release/repro-pack serve --bind 127.0.0.1:8080
# 也可用环境变量指定监听地址：
REPRO_PACK_BIND=0.0.0.0:9000 target/release/repro-pack serve
```

不带子命令时等价于 `serve`。支持 Ctrl-C 优雅关闭。

### 方式 B：命令行一次性打包 / 校验

```bash
repro-pack pack   --source ./src --output-dir ./out --name release --mtime-epoch 0
repro-pack verify --tar ./out/release.tar --source ./src
```

---

## 3. HTTP 接口

所有请求 / 响应均为 JSON。错误体统一为：

```json
{ "error": { "code": "machine_readable", "message": "human readable" } }
```

状态码：`400` 请求不合法（路径不存在、JSON 错误等）；`409` 确定性策略冲突
（规范路径冲突）；`422` 校验未通过；`500` 内部/IO 错误。

### `GET /health`

```bash
curl -s http://127.0.0.1:8080/health
# {"service":"repro-pack","status":"ok"}
```

### `POST /pack`

请求字段：

| 字段           | 类型    | 必填 | 默认       | 说明 |
|----------------|---------|------|------------|------|
| `source`       | string  | 是   | —          | 要打包的源目录（服务端本机路径） |
| `output_dir`   | string  | 是   | —          | 制品输出目录（不允许位于 source 内） |
| `artifact_name`| string  | 否   | `"artifact"`| 制品基名，只能是单一文件名 |
| `mtime_epoch`  | integer | 否   | `0`        | 给**每个**条目盖的固定 mtime（Unix 秒） |
| `overwrite`    | bool    | 否   | `false`    | 是否覆盖已存在制品 |

```bash
curl -s -X POST http://127.0.0.1:8080/pack \
  -H 'Content-Type: application/json' \
  -d '{
    "source": "/tmp/demo/src",
    "output_dir": "/tmp/demo/out",
    "artifact_name": "release",
    "mtime_epoch": 0,
    "overwrite": true
  }'
```

响应（实测）：

```json
{
  "tar_path": "/tmp/demo/out/release.tar",
  "manifest_path": "/tmp/demo/out/release.manifest.json",
  "digest_path": "/tmp/demo/out/release.sha256",
  "sha256": "24c83c85f773f1ded14defbb1d749865efa9822280c33e810173ecd6e03164e2",
  "archive_size": 6144,
  "entry_count": 7
}
```

产出三件制品：

- `<name>.tar`：GNU tar 包；
- `<name>.manifest.json`：内容清单（条目按字节序排序，字段顺序固定）；
- `<name>.sha256`：`<sha256hex>  <name>.tar\n`，与 `sha256sum -c` 兼容。

### `POST /verify`

请求字段：`tar_path`（必填）、`manifest_path`、`digest_path`（后两者缺省时
按同名兄弟文件推导）、`source`（可选；给了就从源目录**重建并逐字节比对**）。

```bash
curl -s -X POST http://127.0.0.1:8080/verify \
  -H 'Content-Type: application/json' \
  -d '{"tar_path":"/tmp/demo/out/release.tar","source":"/tmp/demo/src"}'
```

响应（实测，全部为 true）：

```json
{
  "ok": true,
  "sha256": "24c83c85f773f1ded14defbb1d749865efa9822280c33e810173ecd6e03164e2",
  "archive_size": 6144,
  "entry_count": 7,
  "digest_matches": true,
  "manifest_matches": true,
  "rebuild_matches": true,
  "problems": []
}
```

更多可直接发送的样例见 [`examples/requests.http`](examples/requests.http)。

---

## 4. 确定性规则（规范性说明）

实现位于 `src/pack.rs`，规则如下：

1. **固定排序**。遍历目录后按归档路径的原始字节做字典序排序，根条目 `.` 永远第一。
   不依赖 `readdir` 返回顺序。
2. **规范路径**。每个路径组件必须是合法 UTF-8，且不允许为空、为 `.` 或 `..`、含
   NUL 或 `/`。不做“帮你改写 `..`/`//`”的容错，直接拒绝。归档路径用 `/` 连接，
   根为 `.`，绝不写入源目录的绝对路径。
3. **符号链接规则**。
   - 绝对目标（如 `/etc/passwd`）→ 拒绝；
   - 相对目标先做**纯词法**解析（不触盘，悬空链接也能判定），任何 `..` 越过
     包根（即使之后又绕回根内）→ 拒绝；
   - 目标存在时再做文件系统解析：解析后真实路径必须在根内；
   - **指向目录**的符号链接（含链接套链接、成环）→ 拒绝。打包器只沿真实目录下降，
     因此两个归档路径不可能通过符号链接指向同一个文件；
   - 符号链接成环 → 拒绝（带展开次数上限）。
   - 良性链接（如 `bin/tool -> ../lib/lib.so`，词法不越根）正常保留为 symlink 条目。
4. **硬链接别名**。用 `(st_dev, st_ino)` 识别真实文件身份，两个不同归档路径
   指向同一 inode → 作为规范路径冲突拒绝。
5. **权限映射**（与宿主 umask / 特定位无关）：
   - 普通文件：任一执行位（owner/group/other）置位 → `0755`，否则 `0644`；
   - 目录：`0755`；
   - 符号链接：`0777`；
   - setuid / setgid / sticky 一律剥离。
6. **固定时间戳 / 归属 / 格式**。每个条目都用 GNU tar 头写出：uid=gid=0，
   uname/gname 为空，mtime 统一为 `mtime_epoch`（默认 `0`，即 1970-01-01T00:00:00Z）。
   因此宿主时区不影响归档字节（`tar tvf` 在 UTC+8 机器上显示为 08:00 只是展示层换算）。
7. **文件类型**。只接受普通文件、目录、符号链接；设备、FIFO、socket → 拒绝。
8. **归档写出**。tar 头的 path/size/mode/uid/gid/mtime/entry_type/link_name/checksum
   全部由本程序显式设置，再交给 `tar::Builder::append` 原样写块；末尾固定两个 512 字节
   零块。打包过程中还会对流过的实际字节再算一次 SHA-256 并与扫描期摘要比对，文件在
   打包中途被改动会报错而不是静默产出。

清单 `manifest.json` 用固定字段顺序、2 空格缩进、末尾换行序列化；其本身也随内容确定。

---

## 5. 自动化测试

```bash
cargo test            # debug 构建下运行全部测试
cargo test --release  # release 构建下运行
cargo clippy --all-targets   # lint（当前无警告）
cargo fmt --check            # 格式检查
```

测试组织：

- `tests/pack.rs`（18 个）：空树、排序、权限映射、自定义 mtime、绝对/逃逸/目录/成环
  符号链接、硬链接、特殊文件、输出目录限制、覆盖策略、verify 正常与篡改等；
- `tests/http_api.rs`（7 个）：在进程内驱动 Axum 路由器，覆盖 `/health`、`/pack`、
  `/verify`、409/400/422 状态码与确定性；
- `tests/timezone_e2e.rs`（2 个）：用**真实编译出的二进制**，在 `TZ=UTC`、
  `America/Los_Angeles`、`Asia/Kolkata` 下对 mtime 不同（1980 / 2035 / 2001 年）的
  两份内容相同的树打包，断言 tar 与 manifest **逐字节相同**；并真实 `serve` 起服务、
  发 HTTP 请求、用系统 `tar` 解包校验。

### 实测结果（本机，2026-09-24）

```
running 7 tests  (tests/http_api.rs)  ... test result: ok. 7 passed; 0 failed
running 18 tests (tests/pack.rs)      ... test result: ok. 18 passed; 0 failed
running 2 tests  (tests/timezone_e2e.rs) ... test result: ok. 2 passed; 0 failed
```

合计 **27 passed / 0 failed**；`cargo clippy --all-targets` 无警告；`cargo fmt` 已应用。

确定性对照实验（不同绝对路径 + 不同时区 + 不同 mtime）实测：

```
2ac2487085fa35f31be2d60d466ff69da82af236d7451b543ddc6524c7773ee3  /tmp/det/A/out/pkg.tar   (TZ=UTC,          mtime 1980-...)
2ac2487085fa35f31be2d60d466ff69da82af236d7451b543ddc6524c7773ee3  /tmp/det/B/out/pkg.tar   (TZ=Asia/Kolkata, mtime 2035-...)
BYTE-IDENTICAL: yes
MANIFEST-IDENTICAL: yes
pkg.tar: OK        # sha256sum -c 校验通过
```

规范路径冲突实测（HTTP）：

```
HTTP 409
{"error":{"code":"canonical_path_conflict",
 "message":"canonical path conflict: symlink evil -> ../../etc/passwd escapes the package root"}}
```

---

## 6. 设计取舍与安全说明

- 服务端按请求里的本机路径读写文件，定位为**单机/受信网络内的构建辅助服务**，
  默认只绑定 `127.0.0.1`。它没有（也不需要）多租户鉴权；不要直接暴露到公网。
- 阻塞的文件遍历 / 读写放在 `tokio::task::spawn_blocking` 中执行，不阻塞异步运行时；
  请求体限制 256 KiB（接口只传路径）。
- 制品先写到输出目录下的隐藏临时文件，再原子 `rename` 到位；失败会清理临时文件。
- 输出目录不允许位于源目录内，避免把自己的输出又打进包里。

---

## 7. 未完成项 / 已知限制（如实记录）

1. **路径长度**：沿用 GNU 基础头（路径区 ~100 字节、链接目标区 ~100 字节），未启用
   GNU long-name / PAX 扩展，超长路径会以 `path_too_long` / `link_target_too_long`
   显式报错（而不是悄悄改变编码）。需要时可再加长路径支持，但那会改变归档格式。
2. **非 Linux 平台**：权限位与 `(dev,ino)`、`os_str` 原始字节等按 Unix 设计；
   非 Unix（Windows）做了编译兜底但未在该平台测试。
3. **非 UTF-8 文件名 / 链接目标**：按规则直接拒绝（不做有损转换），以保证清单 JSON
   与排序的确定性。
4. **mtime 仅接受非负 32/64 位秒值**（GNU ustar 数字头不支持负数）；不纳秒、不保留
   源文件时间。
5. 未实现压缩（输出未压缩 `.tar`）。压缩本身（gzip/xz）会引入头部时间等不确定性，
   保持未压缩更利于“相同内容 → 相同字节”的强保证；如需压缩，建议在外部用固定参数
   (`gzip -n`) 处理。
6. 未提供鉴权 / TLS / 多用户隔离（见第 6 节定位）。
