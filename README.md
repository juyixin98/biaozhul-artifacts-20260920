# artifact-delta —— 按需制品差量传输服务（Rust + Axum）

块级（block-level）制品差量传输的纯后端实现。核心思想沿用 rsync：

1. **弱滚动校验（weak rolling checksum）只负责“定位”**——Adler-32 风格的
   32 位滚动和，窗口滑动时可 O(1) 更新，用来在旧文件中快速找候选块；
2. **强摘要（BLAKE3）才负责“判等”**——每个弱校验命中的候选块，必须用
   实际窗口字节的 BLAKE3 摘要再次确认后才当作相同块。

因此：

- 旧文件中**重复块**（弱键映射到多个候选）会逐个做强摘要比对，全部可命中；
- **插入/删除造成的块偏移**由逐字节滚动窗口自然处理，不靠整块跳变；
- **刻意构造的弱校验碰撞**会被强摘要拒绝（见 `constructed_weak_collision_is_rejected_by_strong_hash`
  测试和场景 C），绝不会仅凭弱校验决定内容相等。

应用补丁时还要过三道关：旧文件 BLAKE3（basis 身份）、重建长度、重建结果 BLAKE3。

---

## 1. 依赖与环境

- Rust（实测 `rustc 1.98.1` / `cargo 1.98.1`，edition 2021）
- 无需数据库、无需系统库；纯 HTTP，未启用 TLS（loopback 演示足够）

主要依赖（完整锁定见 `Cargo.lock`）：

| crate | 用途 |
|---|---|
| `axum` 0.8 / `tokio` | HTTP 服务 |
| `blake3` | 强摘要（32 字节） |
| `serde` / `serde_json` | JSON（仅列表/错误响应） |
| `bytes` | 请求体 |
| `tracing` / `tracing-subscriber` | 日志 |
| `ureq` 2（默认特性关闭，无 TLS） | 自带演示客户端 |
| `tower`（dev-dependency） | 集成测试直接调用 Router |

---

## 2. 构建、测试、启动

```bash
# 构建（调试版）
cargo build

# 运行全部自动化测试（单元 + 引擎差量 + HTTP 端到端，共 24 个）
cargo test

# 静态检查（本仓库零 clippy 警告）
cargo clippy --all-targets

# 发布构建
cargo build --release
```

启动服务（默认监听 `127.0.0.1:8080`，可用环境变量覆盖）：

```bash
HOST=127.0.0.1 PORT=8080 cargo run --bin delta-server
# 或直接运行
./target/debug/delta-server
```

启动后访问 `curl http://127.0.0.1:8080/` 可看到接口清单。

一键端到端演示（先启动服务，再另开终端）：

```bash
./scripts/demo.sh
```

脚本会构造 1 MiB 制品，跑“头部插入 / 局部删除 / 弱校验碰撞”三个场景，
打印传输字节与节省比例，并用 `cmp` 校验重建结果逐字节一致。

---

## 3. HTTP 接口

所有制品名只允许 `[A-Za-z0-9._-]`、长度 ≤ 128。制品内容为原始字节。

| 方法 & 路径 | 说明 |
|---|---|
| `PUT  /artifacts/:name` | 上传/覆盖一个制品版本（body 为原始字节） |
| `GET  /artifacts/:name` | 下载制品（响应头带 `x-blake3`、`x-target-length`） |
| `HEAD /artifacts/:name` | 只取长度与 BLAKE3 |
| `GET  /artifacts` | 列出服务端全部制品 |
| `GET  /artifacts/:name/signature?block_len=1024` | 生成该制品的块签名（二进制）；不传 `block_len` 时按 `clamp(len/10000, 64, 65536)` 自动选择 |
| `POST /artifacts/:name/delta` | **核心**：请求 body = 客户端“旧副本”的块签名；响应 body = 二进制补丁，响应头给出复用/传输统计。可选请求头 `x-basis-blake3: <64位十六进制>` 声明旧副本摘要 |
| `POST /artifacts/:name/apply?basis=<旧制品名>&store=true` | body = 补丁，以服务端已存的旧制品为 basis 重建；`store=true` 时把结果存为 `:name` |

`POST /delta` 响应头：

```
x-target-length       新制品总字节
x-basis-length        补丁所基于的旧制品字节
x-blocks-matched      强摘要确认命中的块数
x-bytes-from-basis    从旧内容引用的字节数
x-literal-bytes       必须新传的字面字节数
x-weak-hits           弱校验命中（至少一个候选）的次数
x-strong-rejections   弱键命中但被 BLAKE3 否决的候选次数
x-patch-bytes         补丁本身字节数
x-reuse-percent       bytes_from_basis / target_len
x-blake3              新制品 BLAKE3（十六进制）
```

### curl 请求样例

```bash
B=http://127.0.0.1:8080

# 1) 服务端先有旧版本 v1 和新版本 release
curl -X PUT --data-binary @v1.bin   "$B/artifacts/v1"
curl -X PUT --data-binary @v2.bin   "$B/artifacts/release"

# 2) 取旧版本块签名（实际部署中客户端可本地计算，不必下载）
curl "$B/artifacts/v1/signature?block_len=1024" -o v1.sig

# 3) 只上传签名（~44 字节/块），拿回补丁
curl -D headers.txt -o patch.bin \
     -H 'content-type: application/vnd.artifact-delta.signature' \
     --data-binary @v1.sig \
     "$B/artifacts/release/delta"

# 4) 服务端侧用已存的 v1 应用补丁并存为 rebuilt
curl -X POST --data-binary @patch.bin \
     "$B/artifacts/rebuilt/apply?basis=v1&store=true" -o rebuilt.bin
```

也可以直接用自带客户端跑完整“客户端持有旧文件 → 只收补丁 → 本地重建”的流程：

```bash
cargo run --bin delta-client -- put   "$B" demo v1.bin
cargo run --bin delta-client -- put   "$B" demo-head v2-head.bin
cp v1.bin local.bin
cargo run --bin delta-client -- sync  "$B" demo-head local.bin 1024
# local.bin 已就地更新为新版本，输出各场景的字节节省
```

---

## 4. 二进制线路格式（均为小端序）

### 块签名 `ADLS`

```
magic       4B   b"ADLS"
version     u16  = 1
block_len   u32  块大小 S（1..=65536）
count       u32  块记录数
每条记录（44B）:
  index   u32        块序号（必须从 0 连续）
  length  u32        块长（除最后一块外都等于 S）
  weak    u32        滚动弱校验
  strong  32B        BLAKE3(块内容)
```

### 补丁 `ADLP`

```
magic       4B   b"ADLP"
version     u16  = 1
block_len   u32
target_len  u64  期望重建长度
basis_len   u64
basis_hash  32B  BLAKE3(旧文件) —— 应用前先核对旧副本身份
target_hash 32B  BLAKE3(期望结果) —— 应用后再核对一次
操作流（以 tag 0 结束）:
  0x01 LITERAL  len:u32  bytes[len]
  0x02 REF      index:u32  length:u32
  0x00 END
```

解码时严格校验：magic/版本、字段边界、op 长度累计必须恰等于 `target_len`、
REF 下标不得超出 basis 块数等；应用时再做越界检查与最终强摘要比对。

---

## 5. 弱校验为什么安全（碰撞如何被挡住）

弱校验定义（`S` 为窗口长）：

```
a =  Σ x[i]           mod 65521
b =  Σ (S-i)·x[i]     mod 65521
weak = (b << 16) | a
```

滑动时 `a' = a - x_out + x_in`、`b' = b - S·x_out + a'`，O(1) 滚动。

它**极易碰撞**。测试中构造了长度 5 的两块：

```
A = [10,20,30,40,50]
B = [11,17,33,39,50]   # 增量 (1,-3,3,-1,0)，Δa=0、Δb=0
```

二者弱校验完全相同但内容不同。引擎会在弱键命中后计算窗口的 BLAKE3，
发现摘要不一致即拒绝（统计计入 `x-strong-rejections`），把该块按字面新
字节发送；只有第二块（确实相同）被引用。重建后逐字节一致。

---

## 6. 实际运行结果（如实记录）

环境：`rustc 1.98.1`，Linux，调试构建；演示脚本真实输出。

### 自动化测试

```
cargo test
# src/lib.rs 单元测试 ............ 2 passed（滚动公式 O(1) 与重算一致等）
# tests/engine.rs 引擎测试 ....... 16 passed
#   头部插入 / 块对齐头部插入 / 中部插入 / 局部删除 / 删+插组合 /
#   重复块 / 空文件 / 小于单块 / 短尾块复用 /
#   构造弱校验碰撞 / 错误 basis / 畸形报文 / 200 轮随机变异往返
# tests/http.rs HTTP 集成测试 .... 6 passed
# 合计 24 passed, 0 failed
cargo clippy --all-targets         # 无警告
cargo build --release              # 通过
```

### 端到端演示（`./scripts/demo.sh`，S=1024，1 MiB 随机制品）

**场景 A：头部插入 2 KiB（恰好 2 个块，网格保持对齐）**

```
blocks matched     : 1024
bytes from basis   : 1048576
literal new bytes  : 2048
weak hits          : 1024
strong rejections  : 0
signature bytes    : 45070
patch bytes        : 11360
naive download     : 1050624 bytes (100%)
actually transferred: 56430 bytes (5.37% of naive)
bytes saved        : 994194 (94.63% fewer bytes)
VERIFY: byte-identical OK
```

**场景 B：中部删除 100 KiB（块对齐删除）**

```
blocks matched     : 924
bytes from basis   : 946176
literal new bytes  : 0
signature bytes    : 45070
patch bytes        : 8407
actually transferred: 53477 bytes (5.65% of naive)
bytes saved        : 892699 (94.35% fewer bytes)
VERIFY: byte-identical OK
```

> 若删除/插入长度不对齐块边界，错位区间会有约一个块长的旧字节随字面量
> 一起传输（标准 rsync residue 行为），例如非对齐删除会出现 1 个块的
> literal；重建仍然逐字节一致。引擎测试 `insertion_at_head` 精确断言了
> 这一点（14 字节头 + 64 字节残差）。

**场景 C：构造弱校验碰撞（S=5）**

```
blocks matched     : 1          # 只有真正相同的第二块被引用
bytes from basis   : 5
literal new bytes  : 5          # 碰撞块 B 被当作新内容发送
weak hits          : 2
strong rejections  : 1          # 弱碰撞被 BLAKE3 否决
VERIFY: byte-identical OK
```

文件本身只有 10 字节，签名+补丁的固定开销比整文件还大，属正常现象；
该场景验证的是正确性（不被碰撞欺骗），不是省字节。

### 节省口径

演示客户端把“实际传输”计为 `签名字节 + 补丁字节`（不含 HTTP 头），
“朴素下载”为新制品全量字节。首传或完全无关文件时差量不一定省字节，
这是差量协议的固有特性，客户端可自行比较后二选一。

---

## 7. 代码结构

```
src/
  weak.rs        滚动弱校验（仅定位）
  signature.rs   块签名、二进制编解码、弱键->候选索引（保留重复块）
  delta.rs       滚动匹配引擎：弱定位 + 强确认，处理偏移/重复块/短尾块
  patch.rs       补丁编解码、应用与三重校验（basis 哈希/长度/结果哈希）
  store.rs       内存制品仓库（Mutex<HashMap>）
  error.rs       统一错误类型与 HTTP 状态码映射
  api.rs         Axum 路由与处理器
  main.rs        delta-server
  bin/client.rs  delta-client（put/get/list/sig/sync）
tests/
  engine.rs      15 个引擎/算法测试（含碰撞构造与随机往返）
  http.rs        6 个基于 Router::oneshot 的 HTTP 端到端测试
scripts/demo.sh  真实 TCP 端到端演示
```

---

## 8. 未完成项 / 已知限制（如实说明）

- **存储是进程内 `HashMap`**：重启即丢失，没有持久化与版本管理；
  `/apply?store=true` 只在同一进程内有效。
- **单仓库内 basis 自动识别是线性扫描**：`POST /delta` 未带
  `x-basis-blake3` 时，服务端会遍历已存制品重算签名来匹配旧副本，
  制品多时应改为由客户端显式提供 basis 摘要（协议已支持）。
- **未做流式处理**：签名/补丁/制品整体在内存中（`Bytes`/`Vec<u8>`），
  适合演示与中小制品；超大文件应改为分块流式与异步散列。
- **纯 HTTP、无鉴权、无多租户、无并发写冲突控制**；生产使用需加 TLS、
  认证、配额，并对请求体大小设上限（当前依赖框架默认行为）。
- **补丁编码用 u32 长度字段**，单个字面段与制品大小受 u32 约束；
  块大小上限固定为 65536。
- 演示客户端为简洁起见同步阻塞（ureq），服务端本身是异步的。
- 未做压缩（literal 段可再叠加 deflate/zstd），这是正交优化，未实现。
