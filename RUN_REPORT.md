# 实际运行记录（Run Report）

- 日期：2026-09-23
- 环境：Linux 6.8.0-90-generic (x86_64)，cargo 1.98.1 / rustc 1.98.1，Python 3.12.3
- 目录：`/home/admin/Downloads/biaozhul/opp34/b`
- 依赖：**零第三方 crate**（Cargo.toml 无 `[dependencies]`），仅 Rust 标准库

## 1. 构建

```text
$ cargo build --release
   Compiling mqtt-session-subset v0.1.0
    Finished `release` profile [optimized] target(s) in 1.61s
```

```text
$ cargo clippy --all-targets     # 警告数：0
$ cargo fmt --check              # 通过
```

## 2. 自动化测试：全部通过（46 个）

命令：`cargo test`

| 测试目标 | 用例数 | 结果 |
|---|---:|---|
| 库单元测试（codec/session/topic） | 13 | ✅ 13 passed, 0 failed |
| `tests/parser_tests.rs`（半包/粘包/错误类型/编码字节） | 11 | ✅ 11 passed |
| `tests/acceptance_puback_loss.rs`（PUBACK 丢失→DUP 重发） | 1 | ✅ passed（耗时 2.0s，含重发等待） |
| `tests/acceptance_duplicate_publish.rs`（重复 PUBLISH） | 1 | ✅ passed（0.8s） |
| `tests/acceptance_packet_id_reuse.rs`（包ID复用，入站+出站） | 2 | ✅ passed |
| `tests/acceptance_session_reconnect.rs`（会话重连/CleanSession） | 4 | ✅ passed |
| `tests/acceptance_retained_and_match.rs`（保留消息/通配符） | 4 | ✅ passed |
| `tests/acceptance_subset_boundaries.rs`（拒绝码/QoS2/上限/保活） | 10 | ✅ passed |
| 合计 | **46** | **0 failed** |

## 3. 端到端实跑（release 二进制 + Python 原始字节客户端）

启动：`./target/release/mqtt-subset-broker --addr 127.0.0.1:18830 --retry-ms 1000`

### 3.1 发布者脚本输出（`samples/mqtt_raw_demo.py publisher`）

```text
<- CONNACK session_present=0 return_code=0
[1] 普通 QoS1 PUBLISH demo/temp (id=1001)
<- PUBACK id=1001
[2] RETAIN=1 PUBLISH demo/status
<- PUBACK id=1002
[3] 重复 PUBLISH：相同 id=1003 相同内容发两次
<- PUBACK id=1003 (第 1 次)
<- PUBACK id=1003 (第 2 次)            # 重复也回 PUBACK；订阅端只收到一份
[4] 通配符 demo/humidity（demo/+ 匹配）
<- PUBACK id=1004
[5] PUBACK 丢失模拟
<- (watch) 首次: id=1 dup=False payload=b'resend-me'  [故意不回 PUBACK]
<- (watch) 重发: id=1 dup=True  payload=b'resend-me'  # 相同 id，DUP=1
-> (watch) PUBACK 1，重发循环结束
全部样例完成。
```

### 3.2 订阅者脚本输出（同一轮，CleanSession=0）

```text
<- CONNACK session_present=1 return_code=0     # 重连：Session Present=1
<- SUBACK id=200 codes=[1, 1]                  # demo/temp 与 demo/+
<- PUBLISH topic=demo/status qos=1 id=1 dup=False retain=True  payload=b'online=true'
<- PUBLISH topic=demo/temp   qos=1 id=1 dup=False retain=False payload=b'hello-at-least-once'
<- PUBLISH topic=demo/status qos=1 id=2 dup=False retain=False payload=b'online=true'
<- PUBLISH topic=demo/temp   qos=1 id=1 dup=False retain=False payload=b'dup-body'   # 仅一份
<- PUBLISH topic=demo/humidity qos=1 id=2 dup=False retain=False payload=b'55%'      # + 匹配
<- PUBLISH topic=demo/lost   qos=1 id=1 dup=False retain=False payload=b'resend-me'
```

核对结论：
- **PUBACK 丢失**：重发帧 `dup=True`、包标识符与首帧相同；补 ACK 后停止；
- **重复 PUBLISH**：订阅端只收到一份 `dup-body`，发布端两次都收到 PUBACK；
- **包ID复用**：每会话独立编号，PUBACK 后 id 释放（同日志中 id 被 1/2 循环使用）；
- **保留消息**：新订阅首投 `retain=True`；同主题后续实时投递 `retain=False`；
- **通配符**：`demo/+` 收到 `demo/humidity`（单级）；
- **会话重连**：第二次 CONNACK `session_present=1`。

### 3.3 CONNACK 拒绝码实测（十六进制响应）

```text
协议级别 3（MQTT 3.1）      -> 20 02 00 01   (0x01 Unacceptable Protocol Version)
Will Flag=1                 -> 20 02 00 03   (0x03 Server Unavailable)
空 ClientID + CleanSession=0 -> 20 02 00 02   (0x02 Identifier Rejected)
空 ClientID + CleanSession=1 -> 20 02 00 00   (接受，分配 anonymous-* ID)
```

### 3.4 Rust 示例客户端实测

```text
$ cargo run --example client_demo -- pub ... "ex/demo" 1 "from-rust-example"
CONNACK: session_present=0 return_code=0
PUBLISH sent: topic=ex/demo qos=1 wire_bytes=30
PUBACK received: id=1000

$ cargo run --example client_demo -- sub ... "ex/demo" 1
PUBLISH: topic=ex/demo qos=1 dup=false retain=false id=Some(1) payload="from-rust-example"
  -> PUBACK 1 sent
```

### 3.5 SIGTERM 计数输出

```text
[mqtt-subset] final stats: connect_accepted=1 connect_rejected=0 publish_received=2
  dup_suppressed=0 delivered_qos0=0 delivered_qos1=0 puback=0 puback_unknown=0
  subscribe=0 dropped=0 qos2_rejected=0
```

## 4. 未通过项 / 已知差异（如实记录）

1. **开发过程中出现、已修复并复测通过的问题**（最终代码中均不存在）：
   - 入站去重测试初版让订阅者收到 QoS1 后未确认，触发了*出站*重发，与*入站*
     去重场景混淆——已在测试中让订阅者立即 PUBACK（出站重发由专门用例覆盖）。
   - 通配符用例预置的 QoS1 保留消息未确认，重发帧干扰「无更多消息」断言——
     已在测试中确认保留投递。
   - 一个测试辅助帧在 QoS0 上错误携带了包标识符，导致载荷多出 2 字节；
     broker 按规范把主题后全部字节作为载荷（行为正确），已修正测试构造。
   - SUBACK 剩余长度的测试期望值笔误（3 应为 4），已修正。
   - 包标识符最初用环形游标分配，PUBACK 后不会立刻复用小 ID（需绕满一圈），
     与验收期望不符；已改为「最小可用 ID」策略并补单元测试。
2. **实跑中的一次环境问题**：验证 CONNACK 0x02 时，后台运行的是中途构建的
   旧 release 二进制（曾返回 0x00）；`touch` 强制重新构建并重启进程后，
   三个拒绝码 0x01/0x02/0x03 均实测正确。代码本身与集成测试一致。
3. **能力边界（非缺陷，见 README §2/§8）**：无 QoS2、无遗嘱、无持久化落盘、
   无 TLS/鉴权；语义仅为至少一次，不宣称恰好一次。

## 5. 复现命令汇总

```bash
cargo test
cargo run --release --bin mqtt-subset-broker -- --addr 127.0.0.1:18830 --retry-ms 1000
# 另两个终端：
python3 samples/mqtt_raw_demo.py subscriber 127.0.0.1:18830
python3 samples/mqtt_raw_demo.py publisher  127.0.0.1:18830
```
