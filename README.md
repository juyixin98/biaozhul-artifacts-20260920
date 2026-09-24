# 串口帧增量解析（Rust + Axum）

纯后端项目：自定义二进制串口协议的**真实编码器**、**内存有界的增量解析器**，以及一个
把「录制下来的串口字节流」回放给解析器的 **Axum HTTP 服务**。不需要任何串口硬件。

解析器支持任意切片输入，正确处理：

- **粘包**：一次喂入多帧
- **半包**：一帧被切到任意位置，甚至跨多个 HTTP 请求
- **噪声后重新同步**：帧间任意垃圾字节、假魔数
- **负载内嵌魔数** `DE AD BE EF`
- **坏 CRC 不误吞后续合法帧**
- **超长声明在分配前拒绝**（长度上限先检查，绝不按对端声称的长度分配）
- **内存严格有界**
- **`u16` 序号回绕**，按明确规则报告缺口 / 失序

---

## 1. 协议定义

所有多字节整数均为**大端**（CRC 例外，见下）。

```text
┌───────────┬──────────┬───────────┬───────────────┬──────────┐
│ magic (4) │ len (2)  │ seq  (2)  │ payload (len) │ crc32(4) │
│ DE AD BE EF│  u16 BE  │  u16 BE   │   0..=len     │  u32 LE  │
└───────────┴──────────┴───────────┴───────────────┴──────────┘
```

| 字段 | 含义 |
|---|---|
| `magic` | 固定 `DE AD BE EF`，帧起始定界 |
| `len` | **负载**字节数，`u16`，线上硬上限 65535；服务默认配置上限 `4096`（`DEFAULT_MAX_PAYLOAD`） |
| `seq` | `u16` 帧序号，65535 之后回绕到 0 |
| `payload` | `len` 个原始字节，**可包含魔数**，不做转义 |
| `crc32` | **CRC-32/ISO-HDLC（zlib/PNG）**，多项式反射 `0xEDB88320`，初值/终值 `0xFFFFFFFF` |

CRC 的计算范围是 **`len || seq || payload`**（不含 magic，也不含 CRC 自身）；
4 字节校验和按**小端**存放（与 zlib/PNG 对该反射 CRC 的约定一致）。

CRC 为真实计算：`src/crc.rs` 在编译期生成 256 项查表（const fn），提供一次性与流式两种
接口，并用标准 check 值 `crc32(b"123456789") == 0xCBF43926` 校验。

### 序号回绕 / 缺口规则（明确）

设 `expected` 为下一个期望序号（首帧只建立基线，不报告缺口），
`forward = seq.wrapping_sub(expected)`（mod 65536）：

- `forward == 0`：顺序正确。
- `1 <= forward <= 32767`：报告 `SequenceGap`，`missing = forward`
  （即缺失的序号集合为 `expected ..= seq-1`）。
  - 例：期望 11 收到 13 → 缺 `{11,12}`，`missing=2`。
  - 回绕例：期望 65535 收到 0 → 缺 `{65535}`，`missing=1`；期望 65535 收到 1 → 缺 `{65535,0}`，`missing=2`。
- `forward > 32767`：帧落在序号空间的「过去半边」，判定为迟到 / 重复 / 重排，
  报告 `OutOfOrder`，**不推进** `expected`（帧仍会交付，仅打标）。

### 内存上界（明确）

内部缓冲区恒满足

```text
buf.len() <= 8 + max_payload + 4 + 3        // HEADER + payload + CRC + (MAGIC-1)
```

默认 `max_payload = 4096` 时上界为 **4111 字节**，与喂入块大小、线上 `len` 字段如何夸大
均无关。长度超限的帧在**只读到 8 字节定长头时即被拒绝**，不会等待、更不会分配其声称的
负载长度。

### 重新同步规则（明确）

- 下一个魔数之前的字节 → `Noise` 事件（但会保留至多 3 字节的「魔数后缀」，使魔数跨切片不丢）。
- 超长 `len` → `OversizedLength`，从该魔数的**下一字节**继续扫描（伪负载区里若藏着真魔数能被找到）。
- CRC 错 → `BadCrc`，同样只回退到魔数 +1 重新扫描，**后续字节一律不被吞掉**，因此紧跟的合法帧必然被恢复。

---

## 2. 仓库结构

```text
Cargo.toml                  依赖清单（已生成并提交 Cargo.lock，锁定依赖）
src/
  crc.rs                    CRC-32 真实实现（const 查表 + 标准向量测试）
  codec.rs                  帧常量、Frame、真实编码器 encode_frame
  parser.rs                 增量有界解析器 FrameParser / Event / ParseStats
  sample.rs                 确定性演示字节流（HTTP 与 example 共用）
  http/mod.rs               Axum 路由与 JSON 事件模型
  lib.rs / main.rs          库入口 / 服务二进制入口
examples/
  make_sample.rs            生成示例输入 sample_stream.bin / .hex
  sample_stream.bin/.hex    已生成的示例输入（含噪声/坏CRC/超长/截断）
tests/
  integration.rs            解析器：真实编码器 + 随机切分/损坏模糊测试
  http_api.rs               HTTP 层进程内集成测试
```

---

## 3. 本地构建与启动

需要 Rust（在 1.98 上验证；edition 2021）。

```bash
# 构建（首次会下载并锁定依赖）
cargo build --release

# 启动 HTTP 回放服务（默认 127.0.0.1:8080）
cargo run --release
# 可选环境变量：
HOST=127.0.0.1 PORT=8080 MAX_PAYLOAD=4096 cargo run --release
```

启动后：

```bash
curl -s http://127.0.0.1:8080/
```

### HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET`  | `/` | 服务信息与帧格式 |
| `POST` | `/parse` | body 为**原始字节**，用全新解析器解析并自动 finish；`?max_payload=N` 只能在服务上限内调小 |
| `POST` | `/session` | 创建有状态解析会话（用于跨请求半包） |
| `POST` | `/session/{id}/feed` | 喂任意字节切片，返回本次抽出的事件 |
| `POST` | `/session/{id}/finish` | 结束会话，持留字节报为 truncated |
| `DELETE` | `/session/{id}` | 删除会话 |
| `POST` | `/encode` | JSON `{"sequence":1234,"payload_hex":"deadbeef"}` → `{"frame_hex":...}` |
| `GET`  | `/sample` | 下载确定性演示流（原始字节）；`?format=hex` 返回 JSON |

事件类型（JSON `type`）：
`frame`、`noise`、`bad_crc`、`oversized_length`、`sequence_gap`、`out_of_order`、
`truncated`、`buffer_overflow`（防御性，正常不可达）。
每个事件带绝对字节偏移 `offset`；响应还含累计 `stats`。

---

## 4. 验收命令

> 下面用 `$B` 代表服务地址。先在一个终端启动：
> `PORT=8123 cargo run --release`，另一个终端执行：

### 4.1 生成并回放示例输入（噪声 + 粘包 + 坏CRC + 超长 + 截断 一应俱全）

```bash
cargo run --example make_sample          # 生成 examples/sample_stream{.bin,.hex}

B=http://127.0.0.1:8123
curl -s -X POST --data-binary @examples/sample_stream.bin "$B/parse" | python3 -m json.tool
```

预期 `stats`：

```json
{ "frames_ok": 4, "bad_crc": 1, "oversized": 1, "sequence_gaps": 1, "truncated": 1 }
```

恢复出的帧序号为 `[1, 2, 4, 5]`：seq=3 被故意损坏（坏 CRC），其**紧接的 seq=4 照常恢复**；
一个声称 `len=5000`（超过 4096 上限）的头被拒绝且**不分配**，其后 seq=5 照常恢复；
末尾 seq=6 被截断，报 `truncated` 且 `has_magic=true`。

### 4.2 坏 CRC 不吞后续帧 + 超长不分配（单条断言）

```bash
curl -s -X POST --data-binary @examples/sample_stream.bin "$B/parse" \
 | python3 -c 'import sys,json;d=json.load(sys.stdin);s=d["stats"];assert s["frames_ok"]==4 and s["bad_crc"]==1 and s["oversized"]==1 and s["buffer_overflows"]==0;print("ACCEPT: 坏CRC/超长 后合法帧均恢复，且无缓冲区溢出")'
```

### 4.3 半包跨 HTTP 请求重组

```bash
B=http://127.0.0.1:8123
HEX=$(curl -s -X POST -H 'content-type: application/json' \
  -d '{"sequence":1234,"payload_hex":"deadbeef"}' "$B/encode" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["frame_hex"])')
ID=$(curl -s -X POST "$B/session" | python3 -c 'import sys,json;print(json.load(sys.stdin)["session_id"])')
echo "${HEX:0:16}" | xxd -r -p | curl -s -X POST --data-binary @- "$B/session/$ID/feed"   # 前 8 字节：无帧，buffered=8
echo "${HEX:16}"   | xxd -r -p | curl -s -X POST --data-binary @- "$B/session/$ID/feed"   # 后 8 字节：组出 seq=1234
```

### 4.4 负载内含魔数

```bash
# payload = 00 DE AD BE EF（魔数出现在负载中），仍应完整解出一帧
curl -s -X POST -H 'content-type: application/json' \
  -d '{"sequence":7,"payload_hex":"00deadbeef"}' "$B/encode" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["frame_hex"])' \
  | xxd -r -p | curl -s -X POST --data-binary @- "$B/parse" \
  | python3 -c 'import sys,json;d=json.load(sys.stdin);assert d["stats"]["frames_ok"]==1 and d["events"][0]["payload_hex"]=="00deadbeef";print("ACCEPT: 负载内嵌魔数，单帧完整")'
```

### 4.5 自动化测试（含随机切分/损坏模糊、内存上界、序号回绕）

```bash
cargo test            # 37 个测试：CRC标准向量、编解码、粘包/半包/噪声、
                      # 坏CRC不吞帧、超长分配前拒绝、内存有界、序号回绕、HTTP API
cargo clippy --all-targets    # 0 warnings
```

关键模糊测试（确定性 xorshift PRNG，无需 `rand`，种子可复现）：

- `randomized_roundtrip_many_seeds`（300 个种子）：真实编码若干帧 + 随机噪声，**随机切片**
  喂入，断言每帧按序、负载字节完全一致，并随机制造尾部截断。
- `random_corruption_never_loses_following_good_frames`（200 个种子）：
  `[坏CRC帧][好帧]` 反复拼接 + 随机切片，断言所有好帧序号都被恢复、坏 CRC 计数精确。
- `bounded_memory_under_all_adversarial_streams`（200 个种子，cap=64）：每次喂入后断言
  `buffer_len() <= 8+64+4+3`。
- `long_run_of_pure_noise_never_allocates_hugely`：200 KiB 纯随机噪声，缓冲恒 < 4 字节。

---

## 5. 设计取舍说明

- **定长头先于负载校验长度**：长度在只读到 8 字节时即可判定，超长帧在拿到任何负载字节前
  被拒绝，从根本上杜绝「对端声称 65535 → 被迫大块分配」。
- **CRC 失败只回退 1 字节重扫**，绝不按声称长度整体跳过 —— 这是「坏 CRC 不吞后续合法帧」
  的关键；代价是最坏情况下按字节扫描，复杂度对帧长为线性，可接受。
- **魔数跨切保留**：无魔数时只丢弃到「最长魔数后缀」之前，保证 `...DE | AD BE EF...`
  这种跨 feed 的真魔数不会被当噪声丢掉。
- **无转义**：魔数允许出现在负载中，定长 + CRC 保证帧边界可靠，协议更简单。
- 二进制接口、测试与示例均使用**同一个真实编码器**产生字节，测试不使用手写桩帧，
  从而编码/解析两端始终一致。
