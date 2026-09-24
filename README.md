# artifact-delta — 按需制品差量传输服务（块级 rolling checksum + 强摘要）

纯后端 HTTP 服务，用 **Rust + Axum** 实现 rsync 风格的块级差量制品传输。
旧内容靠 **32 位弱滚动校验和** 在线性时间内定位候选位置，再用 **BLAKE3 强摘要**
逐块确认内容是否真的相等；**弱校验和永远不用于判定内容相等**。支持重复块与
插入/删除造成的任意块偏移，并对补丁结果做端到端强摘要校验。

本仓库只提供 JSON/multipart HTTP 接口，不含任何前端界面。

---

## 1. 它解决什么问题

客户端持有旧版制品（basis），想得到新版制品（target）。与其重新传输整个新文件，
不如只传「新内容」，旧内容以「引用旧文件第 N 块」的方式表达：

- 服务端/发送方对旧制品按固定块大小切块，生成**签名**（每块一个弱校验和 + 强摘要）。
- 接收方在新制品字节流上**滚动**一个等长窗口，用 O(1) 滚动公式更新弱校验和；
  弱校验和命中哈希表只代表「可能是某块」，随即对窗口内容计算 **BLAKE3**，
  与候选块的强摘要比对，完全一致才下发 `copy`，否则该字节按 `literal` 发送。
- 补丁端按 `literal` / `copy` 指令重建新制品，并对整体结果计算 BLAKE3，
  与期望摘要不符直接报错（HTTP 409）。

这从机制上杜绝了「弱碰撞被误判为相同块」：弱校验和仅 16+16 位，碰撞容易构造，
但强摘要（BLAKE3, 256 bit）实际不可碰撞。

### 关键算法要点

- **弱校验和**（rsync 约定，无 Adler 的 `+1` 偏置），窗口长 `n`：

  ```
  a =  Σ X[i]                         (mod 2^16)
  b =  Σ (n-i) X[i]                   (mod 2^16)
  weak = a | (b << 16)
  ```

  窗口右移一字节（丢弃 `X0`，纳入 `Y`）时可 O(1) 滚动：

  ```
  a' = a - X0 + Y
  b' = b - n*X0 + a'
  ```

  滚动恒等式在 `tests/delta_core.rs::roll_step_equivalence_with_rsync_formula`
  与 `checksum::tests::roll_matches_full_computation_every_offset` 中逐偏移校验。

- **强摘要**：每块及整文件使用 [BLAKE3](https://crates.io/crates/blake3)（32 字节）。
  只有强摘要相等才发 `copy`。
- **重复块**：同一弱键下保存全部候选块，命中后逐个强确认，取第一个确认者。
- **插入偏移**：窗口按字节滚动而非按块对齐，头部插入 / 中部插入 / 删除都能重新定位。
- **末尾短块**：主扫描只覆盖整块窗口；剩余不足一块的尾部再以「旧文件最后一个短块
  的长度」做一次短窗滚动扫描，保证尾部短块在插入/删除后仍能被复用（见
  `src/delta.rs` 的 Phase B）。

---

## 2. 依赖与运行环境

- Rust 工具链（开发使用 **rustc/cargo 1.98.1**，edition 2021）。
- 构建期需访问 crates.io 拉取依赖（已提交 `Cargo.lock` 锁定版本）。
- 运行期/演示期：`curl`、`python3`（仅演示脚本用于生成夹具与处理 JSON）、`cmp`。
- 无数据库、无外部服务；制品保存在**进程内存**中（重启丢失，见「已知限制」）。

主要直接依赖（完整传递依赖见 `Cargo.lock`）：

| crate             | 用途                                   |
|-------------------|----------------------------------------|
| axum 0.8 (`multipart`) | HTTP 路由、multipart 表单          |
| tokio 1 (full)    | 异步运行时                             |
| serde / serde_json | 线协议 JSON 序列化                    |
| blake3 1          | 强摘要（内容相等的唯一裁决者）         |
| base64 0.22       | literal 负载在 JSON 中的编码           |
| tracing / tracing-subscriber | 日志                          |
| reqwest 0.12 (dev) | 集成测试 HTTP 客户端                  |

---

## 3. 启动命令

```bash
# 调试运行
cargo run

# 发布构建并运行（推荐，演示脚本会自动构建）
cargo build --release
./target/release/artifact-delta
```

环境变量：

| 变量 | 默认值 | 含义 |
|------|--------|------|
| `ARTIFACT_BIND` | `127.0.0.1:8080` | 监听地址 |
| `ARTIFACT_STORE_MAX_BYTES` | `1073741824`（1 GiB） | 内存制品库总容量上限，超出 PUT 返回 413 |
| `ARTIFACT_MAX_REQUEST_BYTES` | `1073741824`（1 GiB） | 单次请求体上限 |
| `RUST_LOG` | `info` | 日志级别 |

健康检查：`curl http://127.0.0.1:8080/healthz` → `ok`。

---

## 4. HTTP 接口与请求样例

所有 JSON 均通过 `multipart/form-data` 传输，二进制制品可直接作为文件字段。
制品 ID 允许 `[A-Za-z0-9._-]`，长度 1–128。

### 4.1 上传制品

```bash
curl -X PUT --data-binary @old.bin \
  http://127.0.0.1:8080/v1/artifacts/my-old
# 200 {"bytes":262144,"id":"my-old"}
```

### 4.2 下载制品

```bash
curl http://127.0.0.1:8080/v1/artifacts/my-old -o old.bin
```

### 4.3 计算摘要（便于瘦客户端获得期望 BLAKE3）

```bash
curl -X POST --data-binary @new.bin \
  http://127.0.0.1:8080/v1/digests
# 200 {"blake3_hex":"....(64 hex)....","bytes":263264}
```

### 4.4 生成旧制品签名（POST /v1/signatures）

表单字段：`basis_id`、`block_size`（16–65536，默认 1024）。

```bash
curl -X POST \
  -F basis_id=my-old \
  -F block_size=512 \
  http://127.0.0.1:8080/v1/signatures > sig.json
# 200 {"basis_id":..,"basis_bytes":..,"blocks":512,
#      "signature":{"block_size":512,"basis_len":262144,
#                   "blocks":[{"weak":123,"strong_hex":".."}, ...]}}
```

### 4.5 计算差量（POST /v1/deltas）

表单字段：`signature`（**上一步响应里的 `signature` 对象**的 JSON）、
`data`（新制品原始字节）。该端点是**无状态**的：签名自描述，旧制品不必存放在本服务。

```bash
# 取出 signature 对象（演示脚本用 python3，无 jq 依赖）
python3 -c "import json;json.dump(json.load(open('sig.json'))['signature'],open('sig_obj.json','w'))"

curl -X POST \
  -F "signature=@sig_obj.json" \
  -F "data=@new.bin" \
  http://127.0.0.1:8080/v1/deltas > delta.json
```

响应中的 `delta.ops` 是有序指令数组：

```json
{
  "delta": {
    "block_size": 512,
    "basis_len": 262144,
    "ops": [
      {"op": "literal", "data_b64": "SEVEL..."},
      {"op": "copy", "block_index": 0},
      {"op": "copy", "block_index": 1}
    ]
  },
  "stats": { "...": "见第 5 节，传输字节节省报告" }
}
```

同样的统计也以响应头给出：`x-target-bytes`、`x-literal-bytes`、`x-copied-bytes`、
`x-copy-blocks`、`x-delta-wire-bytes`、`x-full-transfer-bytes`、
`x-raw-saved-bytes`、`x-wire-saved-bytes`、`x-wire-saved-percent`。

### 4.6 应用补丁并校验（POST /v1/patch）

表单字段：`basis_id`（服务端保存的旧制品）、`delta`（`delta` 对象的 JSON）、
`expected_blake3_hex`（期望的新制品 BLAKE3）。

```bash
python3 -c "import json;json.dump(json.load(open('delta.json'))['delta'],open('delta_obj.json','w'))"
EXP=$(curl -s -X POST --data-binary @new.bin \
        http://127.0.0.1:8080/v1/digests | python3 -c "import json,sys;print(json.load(sys.stdin)['blake3_hex'])")

curl -X POST \
  -F basis_id=my-old \
  -F "delta=@delta_obj.json" \
  -F "expected_blake3_hex=${EXP}" \
  http://127.0.0.1:8080/v1/patch > patch.json
# 200 {"output_b64":"....","output_bytes":263264,
#      "blake3_hex":"<same as expected>","verified":true}
```

摘要不符返回 **409**（集成测试 `http_wrong_expected_digest_is_rejected` 覆盖）。

### 端点汇总

| 方法 | 路径 | 作用 |
|------|------|------|
| GET  | `/healthz` | 存活检查 |
| PUT  | `/v1/artifacts/{id}` | 上传原始制品 |
| GET  | `/v1/artifacts/{id}` | 下载原始制品 |
| POST | `/v1/digests` | 计算请求体的 BLAKE3 摘要 |
| POST | `/v1/signatures` | 为旧制品生成块签名 |
| POST | `/v1/deltas` | 由签名+新内容计算差量（无状态） |
| POST | `/v1/patch` | 在服务端旧制品上应用差量并强校验 |

错误统一为 `4xx` + `{"error":"..."}`。

---

## 5. 传输字节节省如何计算与报告

差量响应 `stats` 字段（及同名 `x-*` 响应头）含义：

| 字段 | 含义 |
|------|------|
| `target_bytes` | 新制品总字节（=全量传输需要发送的字节） |
| `literal_bytes` | 真正需要发送的**新内容**字节（base64 解码后） |
| `copied_bytes` | 由 `copy` 指令从旧制品复用的字节 |
| `copy_blocks` | 强确认命中的块数 |
| `delta_wire_bytes` | `delta` 对象（block_size/basis_len/ops）JSON 编码后的**实际上线字节** |
| `full_transfer_bytes` | 直接发送整个新文件的字节（=`target_bytes`） |
| `raw_saved_bytes` | `target_bytes - literal_bytes`（内容层面的节省，不含封装开销） |
| `wire_saved_bytes` | **有符号**：`full_transfer_bytes - delta_wire_bytes` |
| `wire_saved_percent` | `wire_saved_bytes / full_transfer_bytes * 100`（可能为负） |

> 注意：线协议是 JSON，`literal` 用 base64 编码（约 +33%），且每个 copy 指令有
> 固定 JSON 开销。当新旧制品几乎没有共享块时，差量可能比全量**更大**。
> 因此 `wire_saved_bytes/percent` 是**有符号**的真实差值，不做「钳到 0」处理。
> 生产环境可在「差量比全量大」时回退为全量发送（当前服务如实报告负值，见下）。

---

## 6. 自动化测试

```bash
cargo test            # 全部：5 单元 + 9 核心算法 + 7 HTTP 端到端
cargo test --release  # release 模式（弱碰撞生日搜索更快）
```

测试构成：

- **单元测试**（`src/` 内）：hex 编解码；弱校验和滚动公式在每个偏移都与
  「整块重算」一致；空块/单字节边界。
- **核心算法**（`tests/delta_core.rs`）：
  - `identical_artifacts_copy_everything`：相同文件零 literal；
  - `head_insertion_shifts_every_block`：**头部插入**后滚动重定位，仅前缀走 literal；
  - `local_deletion_matches_surrounding_blocks`：**局部删除**后后续块仍可复用；
  - `duplicate_blocks_use_first_candidate`：**重复块**经同一弱键全部可发现；
  - `insert_at_non_block_offset_inside_file`：非块对齐的中部/尾部插入；
  - `short_tail_block_matches_after_insertion`：末尾短块两阶段扫描；
  - `random_edits_property_test`：30 组随机插入/删除/改写，块大小随机，全部逐字节还原；
  - **`weak_checksum_collision_is_not_treated_as_match`**：构造两个弱校验和相同、
    BLAKE3 不同的块 P/Q，旧文件放 P、新文件头部放 Q，断言第一条指令必须是
    `literal`，绝不能 `copy` P，且补丁后逐字节一致。
- **HTTP 端到端**（`tests/http_api.rs`）：在随机端口启动真实 Axum 服务，
  跑「上传→签名→差量→补丁→`cmp` 比对」完整链路；头部插入/局部删除报告显著节省；
  **HTTP 全链路弱碰撞**不误配；错误期望摘要返回 409；404/400 等错误路径。

弱碰撞的独立可观察演示（不依赖测试框架）：

```bash
cargo run --release --example weak_collision_demo
```

一键端到端脚本（构建 release、起服务、跑三个场景、逐字节比对）：

```bash
./scripts/demo.sh           # 默认端口 8080
PORT=8099 ./scripts/demo.sh # 指定端口
```

---

## 7. 实际运行结果（本机如实记录）

环境：Linux 6.8.0 x86_64，rustc/cargo 1.98.1。`cargo test` 全绿：

```
test result: ok. 5 passed   (src 单元)
test result: ok. 9 passed   (tests/delta_core.rs，含弱碰撞)
test result: ok. 7 passed   (tests/http_api.rs，含 HTTP 弱碰撞/409)
```

`cargo run --release --example weak_collision_demo`（弱碰撞保证，节选）：

```
block size      : 64
P == Q          : false
weak(P)         : 14bd20cf
weak(Q)         : 14bd20cf  (collision)
strong equal    : false
first delta op  : literal (weak hit rejected by strong hash) ✓
literal bytes   : 64 (Q had to travel)
patched == target : true
```

`./scripts/demo.sh`，256 KiB 高熵确定性夹具，块大小 512（真实输出）：

| 场景 | 目标大小 | literal | 复用块数 | delta 上线字节 | 上线节省 | 逐字节一致 |
|------|---------:|--------:|---------:|---------------:|---------:|:---:|
| 头部插入 +1040B | 263,264 | 1,120 | 512 | 17,846 | **+245,418 B（93.22%）** | ✅ |
| 中部删除 −4096B | 258,048 | 512 | 503 | 16,746 | **+241,302 B（93.51%）** | ✅ |
| 完全不同内容 | 262,144 | 262,144 | 0 | 349,604 | **−87,460 B（−33.36%）** | ✅ |

说明（如实）：第三行差量比全量大约 33%，是 JSON+base64 封装开销所致；
服务**不隐瞒负收益**。真实系统应在 `wire_saved_bytes < 0` 时回退为全量传输，
这属于上层策略，当前库未自动切换（见下）。

---

## 8. 已知限制 / 未完成项

- **无磁盘持久化**：制品仅存内存，进程重启即清空；内存总量受
  `ARTIFACT_STORE_MAX_BYTES` 限制。生产应接对象存储/磁盘缓存。
- **未自动回退全量**：当差量比全量更大（无共享块）时，服务如实给出负节省，
  但不会自动改走全量；需由客户端依据 `wire_saved_bytes` 决策。
- **单轮、固定块大小**：未实现多轮（rsync 的 literal 二次分块）或可变/自适应块长；
  对「插入点之后再无整块对齐」以外的复杂改写，命中率依赖块大小选择。
- **无鉴权/TLS/压缩**：仅监听、无认证、无 HTTPS、无传输层压缩；定位为本地/内网演示。
- **JSON + base64 线协议**：可读性好但不是最紧凑（字面量 +33%）。追求极限带宽可换
  二进制紧凑编码（如长度前缀 + 原始字节），算法层无需改动。
- **并发模型**：内存库用 `RwLock<HashMap>`，单机演示足够；未做多副本/分布式同步。
- 服务端的 `/v1/patch` 要求旧制品已通过 PUT 存放；`/v1/deltas` 则是完全无状态的。

---

## 9. 目录结构

```
Cargo.toml              依赖与构建配置
Cargo.lock              锁定依赖（已提交）
src/
  main.rs               入口、配置、启动
  lib.rs                模块导出
  api.rs                Axum 路由与全部 HTTP 处理
  delta.rs              弱索引、两阶段滚动差量、补丁应用（核心算法）
  checksum.rs           弱滚动校验和 + BLAKE3 强摘要
  protocol.rs           Signature / Delta / Op 线协议类型
  store.rs              内存制品库（带容量上限）
  error.rs              API 错误 → JSON
  hex.rs                hex 编解码
tests/
  delta_core.rs         核心算法 + 弱碰撞测试
  http_api.rs           HTTP 端到端测试
examples/
  weak_collision_demo.rs 弱碰撞独立演示
scripts/
  demo.sh               一键端到端演示
README.md
```
