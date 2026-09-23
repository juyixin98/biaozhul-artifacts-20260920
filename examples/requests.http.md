# 原始 HTTP 请求样例

本文件给出可直接通过 TCP 发送的原始 HTTP/1.1 请求（服务端每连接处理一个请求，
`Connection: close`）。示例假定服务监听 `127.0.0.1:8080`。

参数（均为 query string，URL 编码）：

| 参数    | 默认值  | 说明                                              |
|---------|---------|---------------------------------------------------|
| `job`   | default | 作业 ID，`[A-Za-z0-9_-]{1,128}`                   |
| `mem`   | 65536   | 内存预算（字节，含 I/O 缓冲区）                   |
| `buf`   | 4096    | I/O 缓冲区大小（字节）                            |
| `fanin` | 8       | 多路归并每路最大输入数；会被内存预算进一步钳制    |
| `key`   | `0:asc` | 复合键，`字段:方向`，多段用 `;` 分隔               |

键说明：字段 0 表示整行；字段 n≥1 表示第 n 个制表符分隔字段。
方向为 `asc` / `desc`。所有键相等时按原始输入序号（升序）决胜，即稳定排序。

---

## 1. 健康检查

```http
GET /healthz HTTP/1.1
Host: 127.0.0.1:8080
Connection: close

```

## 2. 提交排序（默认整行升序，小内存）

```http
POST /sort?job=demo1&mem=4096&buf=512 HTTP/1.1
Host: 127.0.0.1:8080
Content-Type: text/plain
Content-Length: 24
Connection: close

banana
apple
cherry
date
```

> 注意：`Content-Length` 必须等于正文实际字节数。最后一行末尾的换行符可有可无。

## 3. 复合键：先按第 2 字段降序，再按第 1 字段升序

键 `2:desc;1:asc` 需 URL 编码为 `2%3Adesc%3B1%3Aasc`：

```http
POST /sort?job=demo2&mem=2048&buf=256&key=2%3Adesc%3B1%3Aasc HTTP/1.1
Host: 127.0.0.1:8080
Content-Length: 20
Connection: close

alpha	3	aa
bravo	1	bb
delta	3	cc
```

（字段间为制表符。）

## 4. 恢复 / 重放同一作业（不带正文）

作业未完成时继续执行，已完成时直接返回结果；参数必须与首次一致，否则 409：

```http
GET /sort?job=demo1&mem=4096&buf=512 HTTP/1.1
Host: 127.0.0.1:8080
Connection: close

```

## 5. 直接取已完成作业的结果

```http
GET /sort?job=demo1&result=1 HTTP/1.1
Host: 127.0.0.1:8080
Connection: close

```

## 6. 用 curl 发送（等价写法）

```bash
# 提交
curl -sS -X POST \
  --data-binary @input.txt \
  "http://127.0.0.1:8080/sort?job=demo3&mem=2048&buf=256&key=2%3Adesc%3B1%3Aasc" \
  -D - -o output.txt

# 查看统计响应头
# X-Sort-Records / X-Sort-Runs / X-Sort-Merge-Rounds / X-Sort-Fanin /
# X-Sort-Peak-Bytes / X-Sort-Runs-Reused / X-Sort-Merges-Reused / X-Sort-Resumed

# 健康检查
curl -sS http://127.0.0.1:8080/healthz
```

## 7. 错误响应样例

```text
HTTP/1.1 400 Bad Request        参数非法（内存过小、键格式错误、job 非法）
HTTP/1.1 404 Not Found          作业不存在 / 结果未生成
HTTP/1.1 409 Conflict           已存在的 job 用不匹配的配置恢复 / POST 带正文重放
HTTP/1.1 500 Internal Server Error  分段 CRC 校验失败等损坏错误
```
