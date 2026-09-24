# smtprecv — 仅回环可用的 SMTP 测试接收器

一个纯后端的 SMTP **接收**测试工具（Go，仅用标准库 `net/http`、`net` 等）：
在本机回环地址上接收邮件并落盘，提供 HTTP 查询接口。**不做任何外发/中继**，
适合本地集成测试、邮件类应用的离线联调。

- SMTP 命令子集：`EHLO` / `HELO` / `MAIL FROM` / `RCPT TO` / `DATA` / `RSET` /
  `NOOP` / `QUIT`（另对 `VRFY/EXPN` 返回 502）
- 严格信封状态机：乱序命令一律 `503`
- DATA 段支持 CRLF 分帧与**点透明**（dot transparency / dot-stuffing，RFC 5321 §4.5.2）
- 仅 **完整收到** `<CRLF>.<CRLF>` 结束序列的邮件才入库；DATA 中途断连、
  正文超过大小上限的邮件一律丢弃，绝不入库
- 原子入库：先写临时文件 + `fsync`，再 `rename(2)` 落到最终文件名，
  崩溃/杀进程不会留下半截邮件
- **回环强制**：SMTP 与 HTTP 都拒绝绑定非回环地址，且运行时二次校验对端 IP
- 纯标准库，零第三方依赖

## 目录结构

```
cmd/smtprecv/main.go     入口（flag 配置、优雅关停）
internal/smtpd/          SMTP 状态机、行分帧、点透明、大小限制
internal/store/          JSON 文件存储，临时文件+rename 原子提交
internal/httpapi/        net/http 查询接口
scripts/acceptance.py    端到端验收脚本（原生 socket 发 SMTP）
tests/                   Go 集成测试（用标准库 net/smtp 客户端打真实链路）
```

## 依赖与启动

依赖：Go 1.23+（仅用标准库，`go.mod` 无任何 require，因此没有 `go.sum`；
这就是"锁定依赖"——构建不依赖网络上的任何第三方模块）。

```bash
# 构建
go build -o bin/smtprecv ./cmd/smtprecv

# 启动（默认 127.0.0.1:2525 收信，127.0.0.1:8080 提供 HTTP）
./bin/smtprecv

# 常用参数
./bin/smtprecv \
  -smtp-addr 127.0.0.1:2525 \
  -http-addr 127.0.0.1:8080 \
  -data-dir  ./smtpdata \
  -hostname  localhost \
  -max-size  1048576 \
  -max-recipients 100 \
  -idle-timeout 2m
```

| flag | 默认值 | 说明 |
|---|---|---|
| `-smtp-addr` | `127.0.0.1:2525` | SMTP 监听地址，必须是回环（`127.0.0.1` / `[::1]` / `localhost`） |
| `-http-addr` | `127.0.0.1:8080` | HTTP API 监听地址，必须是回环 |
| `-data-dir` | `./smtpdata` | 邮件 JSON 落盘目录，启动时自动创建 |
| `-hostname` | `localhost` | 问候语/EHLO 中通告的主机名 |
| `-max-size` | `1048576`（1 MiB） | 单封邮件正文上限（字节） |
| `-max-recipients` | `100` | 单封邮件收件人上限 |
| `-idle-timeout` | `2m` | 命令间空闲超时，到点发 421 关连接 |

绑定非回环地址会直接报错退出，例如：

```
$ ./bin/smtprecv -smtp-addr 0.0.0.0:2525 ...
SMTP config error: refusing to listen on non-loopback address 0.0.0.0
```

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 健康检查 |
| GET | `/messages?limit=N` | 列出最近 N 封（最新在前；列表不含正文） |
| GET | `/messages/{id}` | 获取单封邮件完整 JSON（含正文） |
| GET | `/messages/{id}/raw` | 原始正文，`Content-Type: message/rfc822` |
| DELETE | `/messages/{id}` | 删除一封邮件 |

邮件 JSON 结构：

```json
{
  "id": "1790183317324960662-a0cd90017a5fb96547a428c5",
  "from": "alice@example.com",
  "to": ["bob@example.com", "carol@example.com"],
  "data": "Subject: hi\r\n\r\nbody\r\n",
  "bytes": 22,
  "received_at": "2026-09-23T17:08:37.324975811Z"
}
```

### curl 样例

```bash
curl -s http://127.0.0.1:8080/healthz
curl -s http://127.0.0.1:8080/messages
curl -s http://127.0.0.1:8080/messages?limit=10
curl -s http://127.0.0.1:8080/messages/<id>
curl -s http://127.0.0.1:8080/messages/<id>/raw -o mail.eml
curl -s -X DELETE http://127.0.0.1:8080/messages/<id>
```

## SMTP 请求样例（CRLF 分帧，手工发）

用 `nc`（注意：很多 `nc` 不方便输入 CRLF，推荐用下面的 Python 或真实客户端）：

```
$ python3 - <<'PY'
import socket
s = socket.create_connection(("127.0.0.1", 2525))
f = s.makefile("rb")
def cmd(x):
    s.sendall(x if isinstance(x, bytes) else x.encode())
    print(f.readline().decode().strip())

print(f.readline().decode().strip())      # 220 greeting
cmd("EHLO me\r\n")                         # 250-...
cmd("MAIL FROM:<alice@example.com>\r\n")   # 250 sender ok
cmd("RCPT TO:<bob@example.com>\r\n")       # 250 recipient ok
cmd("RCPT TO:<carol@example.com>\r\n")     # 250 recipient ok（多收件人）
cmd("DATA\r\n")                            # 354
cmd("Subject: test\r\n\r\n")
cmd("a line with a literal dot below\r\n")
cmd("..\r\n")                              # 点透明：线路上的 ".." 还原为 "."
cmd("normal line\r\n")
cmd(".\r\n")                               # 结束序列 -> 250
cmd("QUIT\r\n")
PY
```

用标准库客户端（自动完成 CRLF 与点填充）：

```go
// go run 示例
import "net/smtp"
smtp.SendMail(
    "127.0.0.1:2525", nil,
    "alice@example.com",
    []string{"bob@example.com", "carol@example.com"},
    []byte("Subject: hi\r\n\r\nbody\r\n"),
)
```

协议行为示例（乱序拒绝）：

```
S: 220 localhost SMTP test receiver ready
C: MAIL FROM:<a@x>
S: 503 send EHLO/HELO first
C: EHLO me
S: 250-localhost greets me
S: 250-SIZE 1048576
S: 250-8BITMIME
S: 250 HELP
C: DATA
S: 503 send RCPT first
```

## 自动化测试

```bash
# 全部单元 + 集成测试，带竞态检测
go test -race -count=1 ./...

# 覆盖率
go test -coverprofile=cov.out ./internal/... && go tool cover -func=cov.out

# 端到端验收脚本（先启动服务，且用小的 -max-size 以验证超限场景）
./bin/smtprecv -smtp-addr 127.0.0.1:2525 -http-addr 127.0.0.1:8090 \
  -data-dir /tmp/smtprecv-demo -max-size 1024 &
HTTP_PORT=8090 python3 scripts/acceptance.py
```

`scripts/acceptance.py` 覆盖的验收场景：

1. 完整邮件 + 多个收件人 + 正文含**单点行**与多个点填充行
2. 乱序命令（EHLO/MAIL/RCPT/DATA 前序缺失）→ 503；裸 LF 命令 → 501
3. RSET 清空信封
4. 超限邮件 → 552，且**不入库**
5. DATA 中途 TCP 断开 → **不入库**
6. HELO（旧协议）小邮件仍正常原子入库
7. HTTP：列表/单查/原始报文/删除/404，且列表不含正文

## 设计要点 / 语义说明

- **正文末尾的 CRLF**：按 RFC 5321 §3.3，结束点行之前的那个 CRLF 是最后一行
  内容的行结束符，属于邮件数据，因此存储的 `data` 以 `\r\n` 结尾（标准库
  `net/smtp` 客户端往返字节一致）。
- **超限语义**：超过 `-max-size` 的邮件在读完结束序列后回 `552`，本次事务
  作废（信封被清空，等同 RSET），连接保持可用。为防止滥用，超出上限过多的
  数据流会被主动断开。
- **命令分帧严格、正文分帧宽容**：命令行必须 CRLF 结尾（裸 LF → 501）；
  DATA 正文为兼容真实客户端，也接受裸 LF，并统一规范化存储为 CRLF。
- **收件人去重**：同一信封内大小写不敏感地忽略重复收件人。
- **ID 单调**：ID 前缀是纳秒计数（落盘时维护单调递增，不受系统时钟回拨影响）
  + 12 字节随机后缀；重启后从已有文件名恢复计数。
- **存储格式**：每封邮件一个 `<id>.json`，写入期间为 `<id>.tmp`，
  重启时清理残留 `.tmp`。

## 已知限制 / 未完成项

- 不实现 SMTP-AUTH、TLS/STARTTLS、PIPELINING、CHUNKING/BDAT、SMTPUTF8；
  这是回环测试接收器，不需要鉴权与加密。EHLO 仅通告 SIZE/8BITMIME/HELP。
- 不做收件地址语法的严格 RFC 5321 校验，只做基本形态判断（测试用途足够）。
- 单条 DATA 物理行上限 64 KiB（远宽于 RFC 998 的 1000 字节）；超过按畸形
  输入处理并断开连接。
- 存储为单机文件目录，无压缩、无保留期/自动清理（可用 DELETE 接口清理）。
- HTTP 接口无鉴权，依赖"仅绑定回环"保证只有本机可访问。
