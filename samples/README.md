# RESP 请求样例（原始字节）

这些文件是**线上原始 RESP 字节**，不是文本。用 `xxd` 查看可看到真实的
`0d 0a`（CRLF）。CRLF 在 bulk 头之外是帧分隔符，在 bulk 载荷内部只是数据。

> 约定：下文的 `\r\n` 即字节 `0d 0a`。

| 文件 | 字节（节选） | 说明 |
|---|---|---|
| `01_ping.resp` | `*1\r\n$4\r\nPING\r\n` | 1 元素数组，bulk `PING` |
| `02_set.resp` | `*3\r\n$3\r\nSET\r\n$5\r\nfruit\r\n$5\r\nmango\r\n` | SET fruit mango |
| `03_get.resp` | `*2\r\n$3\r\nGET\r\n$5\r\nfruit\r\n` | GET fruit |
| `04_echo_embedded_crlf.resp` | `*2\r\n$4\r\nECHO\r\n$6\r\nab\r\ncd\r\n` | 第二个 bulk 长度 6，载荷 `ab\r\ncd`——中间的 `\r\n` 是**数据** |
| `05_pipeline.resp` | PING + SET k v + GET k 三帧连写 | 多报文连续输入（pipeline） |
| `06_nested.resp` | `*2\r\n:100\r\n*2\r\n$2\r\nhi\r\n$-1\r\n` | 外层 2 元素：整数 100、内嵌数组 `[bulk hi, null]` |
| `07_empty_and_null.resp` | `$0\r\n\r\n` 紧接 `$-1\r\n` | **空串**帧 + **空值**帧，两者不同 |
| `08_bad_neg_length.resp` | `$-2\r\n` | bulk 负长度，除 `-1` 外非法 |
| `09_bare_lf.resp` | `+OK\n` | 行尾只有 LF 没有 CR |

## 复现

```bash
# 用 TCP 服务（需要命令数组；07/08/09 是顶层裸值，建议用 resp-dump）
nc 127.0.0.1 6379 < samples/01_ping.resp

# 离线解析任意文件，含顶层裸值、非法输入
./target/release/resp-dump --hex samples/04_echo_embedded_crlf.resp
./target/release/resp-dump samples/07_empty_and_null.resp   # 看到 Bulk(len=0) 与 Null
./target/release/resp-dump samples/08_bad_neg_length.resp;  echo "exit=$?"  # 1
```
