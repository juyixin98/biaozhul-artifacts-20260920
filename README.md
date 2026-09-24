# serialframe — 串口帧增量解析库 + HTTP 回放服务

Rust 实现的自定义二进制串口协议解析库（`serialframe`）与 Axum HTTP 回放服务
（`replay-server`）。纯后端，无前端页面，不需要串口硬件。

## 协议格式（全部多字节字段大端）

```
┌─────────┬─────────┬─────────┬─────────┬──────────────┬──────────┐
│ magic0  │ magic1  │ len u16 │ seq u16 │ payload      │ crc32 u32│
│  0xA5   │  0x5A   │         │         │ len 字节     │          │
└─────────┴─────────┴─────────┴─────────┴──────────────┴──────────┘
                 └────────── CRC32 输入：len..payload 末尾 ──────────┘
```

* `len` 仅为负载长度；整帧长 = 6（头） + len + 4（CRC）。
* CRC 为 **CRC-32/IEEE**（多项式 `0xEDB88320`，初值/异或出 `0xFFFFFFFF`，
  与 zlib/PNG/Ethernet 相同），对 `len+seq+payload` 计算。长度字段纳入 CRC，
  长度字节损坏不会误导解析器接受错误负载。
* 默认负载上限 `max_payload = 4096`（可配置，硬上限 65535）。

## 解析器保证（`src/parser.rs`）

* **任意切片**：`feed(&[u8])` 接受任意大小的块（含逐字节），状态保存在
  `Parser` 内；粘包（一块多帧）与半包（一帧跨多块）都支持。
* **噪声重同步**：魔数前的字节作为 `Noise{n}` 事件丢弃；CRC 失败只丢弃
  候选帧的**第一个魔数字节**后重新扫描——因此坏帧不会误吞紧随其后的合法帧，
  负载中即使含有魔数字节（甚至一个完整合法帧）也不会错位。
* **分配前长度检查**：声明长度 > `max_payload` 时发出 `Oversize` 事件并立即
  重同步，**不会**按声明长度分配或等待；缓冲区预分配且永不增长。
* **内存有界**：内部缓冲区上限 `hard_bound = 6 + max_payload + 4` 字节，
  单次 `feed` 的块超过该上限时在**拷贝前**以 `FeedError::ChunkTooLarge`
  拒绝（HTTP 层映射为 413）。持续噪声流下缓冲区也不会超过该界（有测试覆盖）。
* **序号缺口（明确规则）**：序号为 u16，模 65536 回绕。首帧建立期望后，
  按有符号模距离 `d = (recv - expected) mod 65536 ∈ [-32768, 32767]` 分类：
  * `d = 0`：按序，无事件；
  * `1 ≤ d ≤ 32767`：先发出 `Gap{from, to, count=d}`（精确报告丢失个数，
    回绕时如 `from=65535, to=1, count=2`），再交付帧；
  * `d < 0`：发出 `SeqRewind`（旧帧/重复帧），帧仍交付但期望不前移。

## HTTP 回放服务（`src/server.rs`）

每个会话（session）持有独立解析器。所有端点均真实执行解析/编码。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET  | `/` | 服务信息与端点列表 |
| GET  | `/healthz` | 存活与配置 |
| GET  | `/v1/sessions` | 会话列表（缓冲字节数、事件/帧计数、期望序号） |
| POST | `/v1/sessions/{id}/feed` | 追加原始字节（`application/octet-stream`），返回本次产生的事件 |
| GET  | `/v1/sessions/{id}/events?since=N` | 事件日志（`index ≥ N`；日志有界，超出时 `history_partial=true`） |
| GET  | `/v1/sessions/{id}/frames` | 仅已接受帧（payload 以 hex 返回） |
| POST | `/v1/sessions/{id}/reset` | 清空缓冲区、日志与序号状态 |
| POST | `/v1/encode` | `{"seq":N,"payload_hex":"…"}` → 真实编码的帧（hex） |

事件 JSON 的 `type` 取值：`frame` / `noise` / `oversize` / `crc_mismatch` /
`gap` / `seq_rewind`。

服务自身同样有界：会话数上限 64（超出返回 409），每会话事件日志上限 4096
条（淘汰最旧），请求体上限约为一个最大帧（超出返回 413）。

## 构建与启动

```bash
cargo build --release          # 锁定依赖见 Cargo.lock
cargo run --bin replay-server  # 默认监听 127.0.0.1:8080
# 可选环境变量：
#   SERIAL_BIND=0.0.0.0:9000 SERIAL_MAX_PAYLOAD=1024 cargo run --bin replay-server
```

## 验收命令

```bash
# 1) 全部自动化测试（31 个：CRC 向量、随机切分回放、粘包/半包、
#    负载含魔数、截断、超长声明、连续噪声、坏 CRC 恢复、序号回绕、
#    内存有界、HTTP 端到端）
cargo test

# 2) 静态检查
cargo clippy --all-targets

# 3) 生成示例输入（真实编码器产出，含损坏/超长/噪声/回绕场景）
cargo run --example gen_samples

# 4) 启动服务并回放样例
cargo run --bin replay-server &
B=http://127.0.0.1:8080/v1
curl -s --data-binary @samples/01_good.bin              $B/sessions/demo/feed
curl -s --data-binary @samples/03_bad_crc_recovers.bin  $B/sessions/crc/feed
curl -s --data-binary @samples/04_oversize_recovers.bin $B/sessions/ov/feed
curl -s --data-binary @samples/06_split_a.bin           $B/sessions/split/feed   # 半包：无事件
curl -s --data-binary @samples/06_split_b.bin           $B/sessions/split/feed   # 拼合后出帧
curl -s --data-binary @samples/05_seq_wraparound_gap.bin $B/sessions/wrap/feed   # 回绕+缺口
curl -s "$B/sessions/demo/events?since=0"               # 事件日志
curl -s -X POST $B/encode -H 'content-type: application/json' \
     -d '{"seq":7,"payload_hex":"a55a00ff"}'            # 真实编码
head -c 5000 /dev/zero | curl -s -o /dev/null -w '%{http_code}\n' \
     --data-binary @- $B/sessions/big/feed              # → 413
```

## 示例输入（`samples/`，由 `examples/gen_samples.rs` 真实生成）

| 文件 | 场景 | 预期事件 |
|---|---|---|
| `01_good.bin` | 两个连续合法帧（粘包） | `frame` ×2 |
| `02_noise.bin` | 帧前/帧间噪声 + 末尾半个魔数 | `noise`、`frame` ×2，末尾 1 字节留缓冲 |
| `03_bad_crc_recovers.bin` | CRC 损坏帧 + 噪声 + 合法帧 | `crc_mismatch`、`noise`、`frame`（后续帧不误吞） |
| `04_oversize_recovers.bin` | 声明长度 65535 + 合法帧 | `oversize`、`noise`、`frame` |
| `05_seq_wraparound_gap.bin` | 序号 65534→65535→0→2（缺 1） | `frame` ×3、`gap{from:1,to:2,count:1}`、`frame` |
| `06_split_a/b.bin` | 一帧拆两段（半包） | 第一次无事件，第二次出 `frame` |
| `07_payload_has_magic.bin` | 负载内含魔数字节 | 单个 `frame`，不错位 |

## 项目结构

```
src/crc.rs        CRC-32/IEEE（编译期查表，含标准测试向量）
src/parser.rs     增量解析器 + 真实编码器（核心）
src/server.rs     Axum HTTP 回放服务
src/main.rs       服务入口（环境变量配置、优雅退出）
examples/gen_samples.rs  样例生成器
tests/parser_fuzz.rs     解析器集成测试（确定性随机切分等 16 项）
tests/http_api.rs        HTTP 端到端测试（进程内真实路由，8 项）
samples/                 生成的示例输入
```

## 说明

* CRC、编码、解析均为真实实现并真实执行；测试中的"随机"为确定性 PRNG
  （固定种子），失败可复现。
* 已知边界：16 位序号在缺口 > 32767 时按模距离规则归类为"旧帧"
  （`SeqRewind`），这是窗口折半的常规做法，规则见上。
