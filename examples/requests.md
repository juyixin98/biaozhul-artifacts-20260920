# 请求样例（HTTP 报文与 curl）

以下样例针对本地服务 `http://127.0.0.1:8080`，制品 `binary256`
（256 字节，内容为字节 0..255）、`lorem300`（300 字节重复文本）、
`empty`（零长度）。先启动：

```bash
go run ./cmd/rangeserver -addr 127.0.0.1:8080
```

> 报文样例中的 ETag 为示例值；实际值请以 `GET /artifacts` 返回为准
> （ETag 由内容 SHA-256 派生，二进制内容确定，因此示例中的值稳定，
> 但仍建议动态获取）。

## 1. 制品目录

```http
GET /artifacts HTTP/1.1
Host: 127.0.0.1:8080
```

```bash
curl -s http://127.0.0.1:8080/artifacts
```

返回 200 JSON：每项含 `id`、`etag`、`size`、`sha256`、`last_modified`。

## 2. 完整表示（无 Range）

```http
GET /artifacts/binary256 HTTP/1.1
Host: 127.0.0.1:8080
```

```bash
curl -s -D - http://127.0.0.1:8080/artifacts/binary256 -o /tmp/full.bin
```

关键响应头：

```
HTTP/1.1 200 OK
Accept-Ranges: bytes
Content-Type: application/octet-stream
ETag: "…"
Last-Modified: Fri, 02 Jan 2026 03:04:05 GMT
Content-Length: 256
X-Range-Decision: full
```

## 3. 前缀 / 闭区间范围

```http
GET /artifacts/binary256 HTTP/1.1
Host: 127.0.0.1:8080
Range: bytes=0-99
```

```bash
curl -s -D - -H 'Range: bytes=0-99' \
  http://127.0.0.1:8080/artifacts/binary256 -o /tmp/prefix.bin
```

```
HTTP/1.1 206 Partial Content
Content-Range: bytes 0-99/256
Content-Length: 100
X-Range-Decision: single-partial
```

右端越界自动夹紧：`Range: bytes=250-9999` → `206`，
`Content-Range: bytes 250-255/256`，6 字节。

## 4. 后缀范围

```http
GET /artifacts/binary256 HTTP/1.1
Range: bytes=-64
```

```bash
curl -s -D - -H 'Range: bytes=-64' \
  http://127.0.0.1:8080/artifacts/binary256 -o /tmp/suffix.bin
# Content-Range: bytes 192-255/256, 64 字节
```

后缀长度 ≥ 表示长度时回退为整个表示：`Range: bytes=-9999` →
`206 bytes 0-255/256`。

## 5. 开区间（到表示末尾）

```bash
curl -s -D - -H 'Range: bytes=200-' \
  http://127.0.0.1:8080/artifacts/binary256 -o /tmp/tail.bin
# 206, Content-Range: bytes 200-255/256, 56 字节
```

## 6. 多范围（multipart/byteranges）

相邻、不重叠的范围产生多个分段：

```bash
curl -s -D - -H 'Range: bytes=0-99,100-199,200-255' \
  http://127.0.0.1:8080/artifacts/binary256 -o /tmp/multi.bin
```

```
HTTP/1.1 206 Partial Content
Content-Type: multipart/byteranges; boundary=rangereq_…
X-Range-Decision: multipart
```

body 中每段形如：

```
--rangereq_…
Content-Type: application/octet-stream
Content-Range: bytes 0-99/256

<100 字节>
--rangereq_…
Content-Range: bytes 100-199/256

<100 字节>
--…--
```

重叠或重复范围由服务端排序合并：`Range: bytes=0-119,100-255` →
单个 `206 Content-Range: bytes 0-255/256`（不会重复发送重叠字节）。

## 7. 不满足范围 → 416

```bash
curl -s -D - -H 'Range: bytes=1000-' \
  http://127.0.0.1:8080/artifacts/binary256
```

```
HTTP/1.1 416 Range Not Satisfiable
Content-Range: bytes */256
X-Range-Decision: unsatisfiable-416
```

## 8. 零长度制品

```bash
# 普通 GET：200，空体
curl -s -D - http://127.0.0.1:8080/artifacts/empty
# 任何 Range：416 + Content-Range: bytes */0
curl -s -D - -H 'Range: bytes=0-0' \
  http://127.0.0.1:8080/artifacts/empty
```

## 9. If-Range 条件范围

ETag 匹配（用目录里的当前 ETag）：

```bash
ETAG=$(curl -s http://127.0.0.1:8080/artifacts | \
  python3 -c 'import sys,json;print(next(a["etag"] for a in json.load(sys.stdin)["artifacts"] if a["id"]=="binary256"))')

# 匹配 → 206
curl -s -D - -H 'Range: bytes=0-9' -H "If-Range: $ETAG" \
  http://127.0.0.1:8080/artifacts/binary256 -o /tmp/ifok.bin

# 不匹配（陈旧 ETag）→ 忽略 Range，200 完整表示
curl -s -D - -H 'Range: bytes=0-9' -H 'If-Range: "stale"' \
  http://127.0.0.1:8080/artifacts/binary256 -o /tmp/iffull.bin
# X-Range-Decision: ignored-if-range, Content-Length: 256
```

日期形式：

```bash
# 未来日期（晚于 Last-Modified）→ 206
curl -s -o /dev/null -w '%{http_code}\n' \
  -H 'Range: bytes=0-9' -H 'If-Range: Tue, 01 Jan 2030 00:00:00 GMT' \
  http://127.0.0.1:8080/artifacts/binary256      # 206

# 过去日期（早于 Last-Modified）→ 200
curl -s -o /dev/null -w '%{http_code}\n' \
  -H 'Range: bytes=0-9' -H 'If-Range: Sat, 01 Jan 2000 00:00:00 GMT' \
  http://127.0.0.1:8080/artifacts/binary256      # 200
```

## 10. 非法 Range 被忽略 → 200

```bash
curl -s -o /dev/null -w '%{http_code} %header{x-range-decision}\n' \
  -H 'Range: bytes=9-1' \
  http://127.0.0.1:8080/artifacts/binary256
# 200 ignored-malformed
```

## 11. 范围数量超限 → 400

默认上限 5 个范围：

```bash
curl -s -D - -H 'Range: bytes=0-0,1-1,2-2,3-3,4-4,5-5' \
  http://127.0.0.1:8080/artifacts/binary256
# HTTP/1.1 400 Bad Request
# X-Range-Decision: rejected-range-limit
# {"error":"rangespec: too many ranges: 6 requested, limit 5","status":400}
```

## 12. gzip 选定表示（范围按编码后表示计数）

```bash
curl -s -D - --compressed -H 'Accept-Encoding: gzip' \
  http://127.0.0.1:8080/artifacts/lorem300 -o /tmp/lorem.gz
```

```
HTTP/1.1 200 OK
Content-Encoding: gzip
Vary: Accept-Encoding
ETag: "…与 identity 不同的校验器…"
```

对 gzip 表示发范围请求时，`Content-Range` 的总数是**编码后长度**而非
300：

```bash
curl -s -D - -H 'Accept-Encoding: gzip' -H 'Range: bytes=0-9' \
  http://127.0.0.1:8080/artifacts/lorem300 -o /tmp/gzprefix.bin
# 206, Content-Range: bytes 0-9/<gzip 编码长度>
```

拒绝所有可接受编码 → 406：

```bash
curl -s -o /dev/null -w '%{http_code}\n' \
  -H 'Accept-Encoding: identity;q=0, gzip;q=0' \
  http://127.0.0.1:8080/artifacts/binary256             # 406
```

## 13. HEAD 与上传

```bash
# HEAD：只有头，Content-Length 为范围长度
curl -s -I -H 'Range: bytes=0-9' \
  http://127.0.0.1:8080/artifacts/binary256

# 上传实验制品（≤16 MiB）
curl -s -X POST --data-binary @/etc/hostname \
  http://127.0.0.1:8080/artifacts/myhost
```
