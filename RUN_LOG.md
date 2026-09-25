# 本地运行记录（真实执行，未通过项如实列出）

- 日期：2026-09-24
- 环境：Ubuntu, Linux 6.8.0-90-generic；Python 3.12.3；
  `cryptography` 41.0.7；`pytest` 9.1.1
- 代码目录：`/home/admin/Downloads/biaozhul/opp95/a`

## 1. 依赖检查

```text
$ python3 --version
Python 3.12.3
$ python3 -c "import cryptography; print(cryptography.__version__)"
41.0.7
$ python3 -c "import pytest; print(pytest.__version__)"
9.1.1
```

## 2. 自动化测试

```text
$ python3 -m pytest -q
..................................................                       [100%]
50 passed in 8.60s
```

结果：**50 个用例全部通过，无跳过、无失败、无未通过项。**

> 过程中出现过一次服务端 stderr 噪音：`411` 测试用裸 socket 在 HTTP/1.1
> keep-alive 下立即关闭连接，导致服务端等待第二个请求时抛 `ConnectionResetError`。
> 该异常发生在请求已正常响应之后，不影响结果；已在
> `server.py` 的 `handle_one_request` 中捕获连接级异常，重跑后无任何噪音。

测试涵盖的验收点对照：

| 验收要求 | 对应用例 |
| --- | --- |
| 首部范围（首字节） | `test_first_and_last_byte_ranges`、HTTP `Range: bytes=0-0` |
| 尾部范围（尾字节） | 同上（`[34,35)`、`bytes=49-49`） |
| 空对象 | `test_empty_object_*`（磁盘仅 49B 头部、0 块、读回为空） |
| 最后短块 | `test_last_block_short_but_authenticated`、`test_tampering_with_short_last_block` |
| 密文交换 | 块内交换 / 跨对象复制 / 移位 三个用例 + HTTP 409 |
| 篡改不返回未认证数据 | bit-flip、tag、头部长度/块大小、截断、追加、magic |

## 3. CLI 实跑摘录

对象：200 字节、`block_size=64`（4 块：64/64/64/8）。

```text
$ python3 -m enrange put ... --block-size 64
{'id': '0123…cdef', 'length': 200, 'block_size': 64, 'blocks': 4}
$ stat -c '%a' demo-data/master.key
600
$ ls -l objects/0123….bin        # 49 + 3*80 + 24 = 313
-rw------- ... 313 ... 0123….bin
```

范围读取（均与原文件逐字节比对）：

```text
[0,1)   -> 00
[199,200) -> c7
[60,72) -> 3c 3d 3e 3f 40 41 42 43 44 45 46 47
[192,200)-> c0 c1 c2 c3 c4 c5 c6 c7
all 11 range cases match plaintext: True
[0,999) -> invalid range ... ; exit=5
```

空对象：

```text
stat -> {'length': 0, 'block_size': 64, 'blocks': 0}
on-disk size for empty object: 49 bytes
```

篡改与交换（stdout 返回字节数均为 0）：

```text
翻转 block0 密文 1 字节 : full get exit=4 "block 0 authentication failed"
                         区间[0,8)   exit=4
                         区间[192,200)（未触及块）仍返回正确尾部 c0..c7
同对象交换 block0/block1: exit=4 "block 0 authentication failed"
改头部 plaintext_len    : exit=4 "encapsulated length does not match metadata"
```

## 4. HTTP / curl 实跑摘录

服务：`python3 -m enrange serve --host 127.0.0.1 --port 8099`。

```text
POST /objects?block_size=16            -> 201，返回服务端 id
GET  /objects/{id}                     -> 200, Content-Length: 50
HEAD /objects/{id}                     -> 200, X-Enrange-Blocks: 4
Range: bytes=0-0                       -> 206, Content-Range: bytes 0-0/50, body "X"
Range: bytes=49-49                     -> 206, Content-Range: bytes 49-49/50, body "X"
Range: bytes=40-                       -> 206, 到末尾
GET ?start=14&end=20                   -> 206, Content-Range: bytes 14-19/50, "XXXXXX"
PUT 空对象 / GET 空对象                 -> 201 / 200, Content-Length: 0
未知 id                                -> 404 {"error":"not_found"}
?start=0&end=9999                      -> 416 {"error":"bad_range"}
多区间 Range: bytes=0-1,3-4            -> 400/416（拒绝）
```

篡改后的 HTTP 行为：

```text
on-disk 翻转 1 字节后  GET 全量          -> 409 {"error":"authentication_failed"}
区间触及被改块 [0,4]                    -> 409
区间仅取已验证尾部 [192,200)            -> 206，正文正确
跨对象复制 block2 帧后 GET 全量 / 区间   -> 409 / 409
还原文件后 GET                         -> 200
```

样例脚本同样实跑通过：

```text
$ python3 examples/http_client_demo.py
created {... 'length': 50, 'block_size': 16, 'blocks': 4}
full   200 50
first  206 b'Z' bytes 0-0/50
last   206 b'Z' bytes 49-49/50
slice  206 b'ZZZZZZ' bytes 14-19/50
meta   200 {...}
empty  200 0 len-header= 0
$ bash examples/curl-examples.sh        # 200/201/206/404/416 均符合预期
```

## 5. 备注 / 已知限制

- 开发时 8080 端口被环境中其他进程占用，演示改用 8099/8100；与程序无关。
- 服务仅监听 localhost，无 TLS / 鉴权；`PUT` 整体重写对象（原子 rename）。
- 演示生成的 `demo-data/` 包含本地测试主密钥，仅用于本机验证，不要分发。
