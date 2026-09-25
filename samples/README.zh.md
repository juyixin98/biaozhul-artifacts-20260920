# 请求样例（原始二进制帧）

每个 `.bin` 文件就是**一个完整的线路帧**（24 字节头部 + payload），可原样写入 TCP。
机器生成的索引见 [README.md](README.md)。

| 文件 | 方法 | request_id (hex) | 含义 |
|---|---|---|---|
| request_echo.bin | Echo(1) | `0000000100000001` | 原样返回 `hello-rpc` |
| request_slow.bin | Slow(2) | `0000000100000002` | 延迟 150ms 后返回 `later` |
| request_add.bin | Add(3) | `0000000100000003` | 40 + 2 = 42 |
| request_stats.bin | Stats(4) | `0000000100000004` | 返回 7×u64 统计快照 |

payload 第一个 u16 是方法号；Slow 之后 4 字节是延迟毫秒（`00 00 00 96`=150）；
Add 之后是两个大端 i64。

## 发送方式

先启动服务端：`./target/release/brpc-server 127.0.0.1:9000`

**python（最稳妥）**

```python
import socket, struct
d = open("samples/request_echo.bin","rb").read()
s = socket.create_connection(("127.0.0.1", 9000), timeout=3)
s.sendall(d)
# 先收 24 字节头，按 payload_len 收体
def recvn(n):
    b=b""
    while len(b)<n: b+=s.recv(n-len(b))
    return b
hdr=recvn(24)
plen=struct.unpack(">I",hdr[16:20])[0]
body=recvn(plen)
print(hdr[:4], "kind=",hdr[5], "code=",struct.unpack(">H",body[:2])[0], body[2:])
# b'BRPC' kind= 2 code= 0 b'hello-rpc'
```

**netcat（某些发行版需要 `-N`/`-q1`，行为有差异）**

```bash
cat samples/request_echo.bin | nc 127.0.0.1 9000 | od -An -tx1
```

也可用自带客户端发等价请求：`./target/release/brpc echo hello-rpc 127.0.0.1:9000`
（客户端会自行分配带代次的 request_id，与样例中的固定 id 不同）。
