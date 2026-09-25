# delta-update HTTP 请求样例

本目录包含本地 JSON 接口的完整请求样例，以及对应的 curl 脚本（见
`requests.sh`）。服务默认只监听 `127.0.0.1`，不连接任何云平台。

所有 `{hash}` 均为 64 位小写十六进制 SHA-256。

## 1. 健康检查

```http
GET /v1/health HTTP/1.1
```

```json
{"status":"ok"}
```

## 2. 上传制品到内容寻址缓存（原始字节流）

```http
POST /v1/artifacts HTTP/1.1
Content-Type: application/octet-stream

<原始字节流>
```

```json
{ "hash": "ac33f6ff49fbae4a3015e14a9cc83d20c1c023f679f1ce36f3269f3ce2576a3c8",
  "size": 73728 }
```

## 3. 从缓存下载制品

```http
GET /v1/artifacts/ac33f6ff...a3c8 HTTP/1.1
```

响应为 `application/octet-stream`，并带 `X-Content-Sha256` 头。

## 4. 生成块级差量补丁（旧新引用均支持“缓存哈希”或“绝对路径”）

```http
POST /v1/deltas HTTP/1.1
Content-Type: application/json

{
  "oldRef": "ac33f6ff49fbae4a3015e14a9cc83d20c1c023f679f1ce36f3269f3ce2576a3c8",
  "newRef": "51385523e7e6eb18b880c40e74c72d510f7db6cda3225fd16e61da3208668e5e",
  "blockSize": 1024
}
```

响应（`patch-payload.example.json` 是同样结构的完整文件样例）：

```json
{
  "patch": {
    "version": "delta-patch/v1",
    "blockSize": 1024,
    "oldSize": 73728,
    "newSize": 73759,
    "oldSum": "ac33...a3c8",
    "newSum": "5138...8e5e",
    "ops": [
      { "type": "copy", "oldIndex": 0 },
      { "type": "data", "data": "Pj4+Q1JBU0gtRklYVFVSRS1JTlNFUlRJT048PDw=" },
      { "type": "copy", "oldIndex": 1 }
    ],
    "patchSum": "8a3f...（对除 patchSum 外整个补丁规范化 JSON 的 SHA-256）"
  }
}
```

补丁**同时绑定旧摘要与新摘要**：`oldSum` 用于拒绝错基线，`newSum`
用于在替换前核验重建结果；`patchSum` 用于在应用前检测补丁自身被截断/篡改。

## 5. 原子应用到工作目录中的目标（相对工作目录的路径）

```http
POST /v1/apply HTTP/1.1
Content-Type: application/json

{
  "target": "builds/app1/current.bin",
  "patch": { ...第 4 步返回的 patch 对象... }
}
```

成功：

```json
{ "applied": true,
  "oldDigest": "ac33...a3c8",
  "newDigest": "5138...8e5e" }
```

重复应用（崩溃后幂等恢复，HTTP 语义仍为 200）：

```json
{ "applied": false,
  "oldDigest": "5138...8e5e",
  "newDigest": "5138...8e5e" }
```

错误响应统一形如：

```json
{ "error": { "code": "wrong_base", "message": "apply: wrong base artifact: ..." } }
```

| HTTP | code | 触发条件 |
|-----|------|---------|
| 409 | `wrong_base` | 磁盘上制品摘要 ≠ 补丁 `oldSum` |
| 422 | `corrupt_patch` | 补丁 `patchSum` 校验失败（损坏/被篡改） |
| 507 | `insufficient_space` | 空间预检发现可用空间不足 |
| 409 | `locked` | 同一目标正被另一个应用进程锁定 |
| 400 | `bad_target` / `bad_request` | 路径越界、参数非法 |

## 6. 查看工作目录中目标的摘要与大小

```http
GET /v1/work/builds/app1/current.bin HTTP/1.1
```

```json
{ "path": "builds/app1/current.bin", "size": 73759,
  "sha256": "51385523e7e6eb18b880c40e74c72d510f7db6cda3225fd16e61da3208668e5e" }
```

## 7. 预置/覆盖工作目录中的目标（夹具与演示用，原子写入）

```http
PUT /v1/work/builds/app1/current.bin HTTP/1.1
Content-Type: application/octet-stream

<原始字节流>
```

## 8. 测试夹具专用：故障与空间注入

硬崩溃（`os.Exit`）**只**通过 CLI 的环境变量触发，绝不通过 HTTP；HTTP
提供的是进程内可恢复故障与空间模拟，便于自动化验收：

```http
POST /v1/apply HTTP/1.1
{ "target": "builds/app1/current.bin",
  "patch": { ... },
  "failStage": "write-delta",
  "simFreeBytes": 100 }
```

`failStage` 取值：`check-old`、`space`、`prepare`、`write-delta`、`sync`、
`verify-delta`、`rename`、`post-rename`、`fsync-dir`、`verify-new`。
故障后旧制品保持可用；再次正常应用即收敛。
