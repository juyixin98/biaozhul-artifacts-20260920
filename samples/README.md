# 请求样例

本目录提供可直接复现的线路字节样例与独立工具。

## binary/ — 原始帧文件（可直接 `sock.sendfile` / `nc` 管道）

| 文件 | 内容 |
|---|---|
| `01-echo.bin` | REQUEST id=1 ECHO "hello rpc" |
| `02-upper.bin` | REQUEST id=2 UPPER "mixed Case 123" |
| `03-add.bin` | REQUEST id=3 ADD(40, 2)（两个大端 u64） |
| `04-slow-1000ms.bin` | REQUEST id=4 SLOW 1000ms "late-body"（用于超时/迟到测试） |
| `05-fail.bin` | REQUEST id=5 FAIL（应用错误） |
| `06-ping.bin` | PING id=6 |
| `10-sticky-three-requests.bin` | **三个帧粘在同一缓冲**（粘包样例） |
| `11-half-frame-truncated.bin` | 被截断 4 字节的帧（半包/EOF 样例） |
| `12-crc-corrupted.bin` | 载荷被翻转一字节、CRC 未更新（致命错误样例） |
| `20-response-echo.bin` | RESPONSE id=1 应用状态 OK "hello rpc" |
| `21-error-unknown-command.bin` | ERROR 帧，code=5 UnknownCommand |
| `22-cancel-id16.bin` | CANCEL id=16 |
| `23-pong.bin` | PONG id=6 |

## hex/ — 带字段标注的人类可读样例

由 `frame_tool.py hexdump` 生成，逐字段标注 magic/version/flags/command/
request_id/payload_len/payload/crc32 及偏移。

## scripts/

- `frame_tool.py`：纯 Python 标准库实现（struct/socket/zlib），与 Rust 实现
  无任何共享代码，用于独立互操作验证与故障注入。子命令：
  `hexdump | send | soak | inject | dump`（详见 README §6.5 或 `--help`）。
- `run_acceptance.sh`：一键自动化验收，日志写入 `test-results/acceptance.log`，
  结果写入 `test-results/status.txt`。

### 用 nc 风格方式发送样例（bash）

```bash
# 先启动 ./target/release/demo-server --bind 127.0.0.1:9000
python3 samples/scripts/frame_tool.py dump samples/binary/01-echo.bin   # 只看结构
python3 - <<'PY'
import socket
s = socket.create_connection(("127.0.0.1", 9000))
s.sendall(open("samples/binary/01-echo.bin","rb").read())
print(s.recv(4096).hex(" "))
PY
```
