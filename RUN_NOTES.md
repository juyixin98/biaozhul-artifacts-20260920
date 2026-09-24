# 实际运行记录

- 时间：2026-09-24
- 环境：Linux 6.8 / Python 3.12.3 / 依赖按 `requirements*.txt` 精确锁定版本
- 启动（演示配额）：

```bash
ARCH_DATA_DIR=/tmp/ag-final \
ARCH_MAX_TOTAL_BYTES=10000000 \
ARCH_MAX_FILE_BYTES=10000000 \
ARCH_MAX_COMPRESSION_RATIO=30 \
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
```

## 自动化测试

```
$ .venv/bin/python -m pytest tests/
44 passed, 1 warning in 0.63s
```

完整输出见 `docs/demo_output/pytest.txt`。唯一警告来自 starlette 对
`httpx` TestClient 的弃用提示，不影响功能。

## 真实 HTTP 调用结果

完整会话记录见 `docs/demo_output/live_curl.txt`，要点：

| 场景 | 结果 |
| --- | --- |
| `GET /health` | `200 {"ok": true, ...}` |
| 正常 tar 解包（目录+文件+符号链接） | `200`，发布，`bytes_written=19`，文件 0600/目录 0700 |
| 预检 precheck | `200`，`extracted/` 下不产生任何解包目录 |
| 绝对路径 `/tmp/pwned-absolute` | `422 absolute_path` |
| 目录穿越 `../../rel-evil`（同一归档） | 首条绝对路径即被拒绝（顺序拒绝） |
| 符号链接 `escape -> ../../../etc` + 经链接写入 | `422 link_escape` |
| gzip 炸弹（2 MB 零压缩后 2130 字节，比值 984.6 > 30） | `422 compression_bomb` |
| Ed25519 正确签名 | `200` |
| 篡改 1 个签名 hex 字符 | `401 invalid_signature` |
| 带签名但不给公钥 | `401 missing_public_key` |

## 验收点核对

1. **目标根外无写入**：恶意请求后检查 `/tmp/pwned-absolute`、
   `/tmp/pwned-traversal`、`/etc/pwned` 均不存在；
   测试 `test_no_write_outside_root`、`test_quarantine_cannot_escape_via_link`
   自动化覆盖。
2. **失败后不发布半成品目录**：失败时 `data/extracted/` 前后目录集合一致；
   无 `.staging-*` 残留；隔离文件被删除（`test_extract_failure_is_atomic`、
   `test_failure_publishes_nothing`）。
3. **配额计入实际写出字节**：文件按 1 MiB 块写出，逐块与总配额/单文件上限
   比较；响应与 `.manifest.json` 中的 `bytes_written` 只统计真实写出数据。
4. **链接链**：自环、互环、20 连长链、硬链接环均被拒绝
   （`symlink_loop`）；合法的目录别名符号链接与悬空相对链接可正常发布。

## 已知限制 / 未完成项

- 仅支持 tar 及 tar.gz/tar.bz2/tar.xz；不支持 zip、AR、ISO 等其他归档格式。
- 没有内置身份认证/多租户；Ed25519 验签是可选的数据来源校验，不是用户登录。
- 发布目录无自动清理/TTL，也没有“下载已解包文件”的接口（按“纯后端安全检查
  服务”的范围有意省略，避免把服务变成文件托管）。
- 未做并发资源治理（全局信号量/磁盘预留）：单次请求的配额是严格的，但没有
  限制并发请求总数；高并发下需要在反向代理或外层限流。
- 未引入 fuzzing/属性测试；现有 44 个用例为定向夹具测试。
- 未配置 ASGI 多进程/容器镜像/CI 流水线文件；`uvicorn` 单进程开发服务器即可
  满足本次验收，生产部署方式（gunicorn/容器/系统服务）按环境另行决定。
