# HTTP 请求样例

以下样例假设缓存服务在 `http://127.0.0.1:8080`、构建服务在
`http://127.0.0.1:8081`。`<DIGEST>` 形如
`sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`。

用 curl 计算文件摘要：

```bash
DGST="sha256:$(sha256sum model.bin | awk '{print $1}')"
echo "$DGST"
```

## 缓存服务

### 健康检查

```bash
curl -s http://127.0.0.1:8080/healthz
# { "status": "ok" }
```

### 上传对象（先暂存、校验摘要、原子发布）

```bash
curl -s -X PUT \
  --data-binary @model.bin \
  "http://127.0.0.1:8080/v1/blobs/$DGST"
```

成功：

```json
{ "digest": "sha256:…", "size": 4096, "existed": false }
```

再次上传相同内容：`"existed": true`。

### 摘要不符（坏上传被拒绝并隔离）

```bash
BAD="sha256:$(printf 'not-the-content' | sha256sum | awk '{print $1}')"
curl -s -i -X PUT --data-binary @model.bin \
  "http://127.0.0.1:8080/v1/blobs/$BAD"
```

```
HTTP/1.1 400 Bad Request
```

```json
{
  "error": "upload rejected",
  "code": "digest_mismatch",
  "detail": "digest mismatch: claimed <bad> got <actual>"
}
```

该对象随后 `GET` 返回 404；字节出现在 `<cache-dir>/quarantine/`。

### 超过大小上限

```bash
curl -s -i -X PUT --data-binary @huge.bin \
  "http://127.0.0.1:8080/v1/blobs/sha256:$(sha256sum huge.bin | awk '{print $1}')"
# HTTP/1.1 413 Request Entity Too Large
# { "code": "object_too_large", ... }
```

### 元数据（HEAD）

```bash
curl -sI "http://127.0.0.1:8080/v1/blobs/$DGST"
# ETag: "<hex>"
# X-Content-Digest: sha256:…
# X-Content-Length: 4096
# Accept-Ranges: bytes
```

### 下载与断点续传（Range）

```bash
# 全量
curl -s "http://127.0.0.1:8080/v1/blobs/$DGST" -o model.download.bin

# 只取前 1 KiB（中断后续传所用的原语）
curl -s -i -H 'Range: bytes=0-1023' \
  "http://127.0.0.1:8080/v1/blobs/$DGST" -o first1k.bin
# HTTP/1.1 206 Partial Content
# Content-Range: bytes 0-1023/<total>

# 从偏移量续传
curl -s -H 'Range: bytes=1024-' \
  "http://127.0.0.1:8080/v1/blobs/$DGST" -o rest.bin
```

### 服务端校验 / 诊断坏缓存

```bash
curl -s -X POST "http://127.0.0.1:8080/v1/blobs/$DGST/verify"
# { "digest": "…", "size": 4096, "ok": true }
```

对象损坏时：

```
HTTP/1.1 500 Internal Server Error
```

```json
{
  "error": "verification failed",
  "code": "corrupt",
  "detail": "stored object is corrupt: sha256:…",
  "extra": {
    "quarantined": true,
    "size": 4096,
    "quarantineDir": "./cache/quarantine"
  }
}
```

### 列表 / 统计 / 删除

```bash
curl -s http://127.0.0.1:8080/v1/blobs | jq .
curl -s http://127.0.0.1:8080/v1/stats | jq .
curl -s -X DELETE "http://127.0.0.1:8080/v1/blobs/$DGST"
```

## 构建服务

### 列出允许的夹具（唯一可执行集合）

```bash
curl -s http://127.0.0.1:8081/v1/fixtures | jq .
```

### 启动构建（只能给夹具名）

```bash
curl -s -X POST http://127.0.0.1:8081/v1/builds \
  -H 'Content-Type: application/json' \
  -d '{"fixture":"make-model"}'
```

```json
{ "id": "a1b2c3…", "fixture": "make-model", "status": "queued", ... }
```

### 等待终态 / 查询

```bash
JOB=<id>
curl -s "http://127.0.0.1:8081/v1/builds/$JOB?wait=30s" | jq .
```

成功（产物已上传缓存）：

```json
{
  "id": "…",
  "fixture": "make-model",
  "status": "succeeded",
  "exitCode": 0,
  "artifacts": [
    { "name": "model.bin", "path": "model.bin",
      "digest": "sha256:…", "size": 4096 }
  ]
}
```

失败夹具（退出码被如实记录，不产生任何缓存对象）：

```json
{
  "fixture": "fail-fixture",
  "status": "failed",
  "exitCode": 7,
  "error": "fixture \"fail-fixture\" exited with code 7",
  "logsTail": "fail-fixture: simulating a broken build\n…"
}
```

### 命令注入 / 任意命令被拒绝

```bash
curl -s -i -X POST http://127.0.0.1:8081/v1/builds \
  -H 'Content-Type: application/json' \
  -d '{"fixture":"rm -rf /; echo pwned"}'
# HTTP/1.1 400 Bad Request
# { "code": "unknown_fixture", ... }
```
