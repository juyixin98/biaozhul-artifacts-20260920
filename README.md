# Archive Guard —— 归档解包安全检查服务

纯后端服务（Python 3.12 + FastAPI + cryptography），对上传的 tar 归档做**上传预检**与**隔离解包**。
不提供任何前端页面，仅暴露 HTTP JSON 接口。

## 威胁模型与防护

归档内容视为完全不可信，防护以下攻击：

| 威胁 | 处理方式 |
|---|---|
| 绝对路径（`/etc/passwd`） | 成员名以 `/` 开头直接拒绝 |
| 目录穿越（`../../x`、`foo/../..`） | 逐组件词法规范化，出现 `..` 即拒绝；`//`、空组件、NUL、控制字符、反斜杠同样拒绝 |
| 符号链接逃逸 | 链接体按 POSIX 语义相对其所在目录规范化；绝对目标、词法上跳出根目录的目标拒绝 |
| 符号链接链 / 环 / 链接当目录 | 先在**内存中的抽象链接图**上做内核式逐组件解析（带跳数上限），任何非链接成员解析后路径与原路径不一致即拒绝；环和超长链拒绝 |
| 硬链接逃逸 | 硬链接目标必须是归档内已存在的普通文件，且同样经过路径规范化；拒绝指向归档外的硬链接 |
| 设备/FIFO 等特殊成员 | 仅接受 `普通文件/目录/软链接/硬链接`；字符设备、块设备、FIFO、contiguous、GNU sparse 等拒绝 |
| 解压炸弹 | 条目数上限、单文件大小上限、**解包时按实际写出字节计数的总配额**、压缩包大小上限、解压比（uncompressed/compressed）上限 |
| 重复成员名 | 规范化后重名拒绝（防止覆盖/TOCTOU） |
| setuid/setgid/sticky 位 | 写出时一律清除 |

支持的 tar 子集：**未压缩 tar** 或 **gzip 压缩 tar**（ustar/PAX/GNU 头）；明确不接受 bz2/xz 等其他压缩格式。

### 原子发布（失败后不发布半成品目录）

解包先写入隐藏的暂存目录 `.staging.<pid>`：

1. 暂存目录以 `0700` 创建，完整重放预检的全部校验；
2. 普通文件用 `O_CREAT | O_EXCL | O_NOFOLLOW` 写出，分块计数，超出单文件/总配额立即中止；
3. 父目录按需创建为**真实目录**（若已是符号链接/文件则拒绝，纵深防御）；
4. 全部成功后 `os.rename()` 原子改名为正式目录；
5. 任何异常（含配额、损坏、中断）都会删除整个暂存目录，正式目录永不出现。

**配额按实际写出字节计算**：流式读取 tar 负载时逐块累加 `written`/`total_written`，
而不是只信任 tar 头声明的大小；头声明大小与实际读到的字节数不一致（截断）也会报错。

## 目录结构

```
app/
  main.py                 # FastAPI 路由：/health、/api/v1/inspect、/api/v1/extract
  config.py               # 配置（环境变量覆盖）+ 上传假脱机文件
  schemas.py              # Pydantic 响应模型
  archive_guard/
    safety.py             # 核心：名称/链接规范化、抽象链接图解析、预检、隔离解包
    limits.py             # 所有配额/策略上限
    errors.py             # 带稳定错误码与 HTTP 状态码的异常
    hashing.py            # 基于 cryptography 的流式 SHA-256
tests/
  test_safety.py          # 安全夹具测试（恶意路径、链接链、炸弹、无越界写入）
  test_api.py             # HTTP 接口测试
  tarbuild.py             # 内存构造 tar 夹具的工具
requirements.txt          # 运行依赖（精确锁定版本）
requirements-dev.txt      # 测试依赖
```

## 依赖（已锁定）

运行：见 `requirements.txt`（fastapi、uvicorn[standard]、python-multipart、cryptography，均钉死版本）。
测试：见 `requirements-dev.txt`（额外 pytest、httpx）。Python 要求 3.10+（开发与验证使用 3.12.3）。

SHA-256 使用 `cryptography` 包的 `cryptography.hazmat.primitives.hashes`（非 hashlib）。

## 启动

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt   # 仅运行服务可只装 requirements.txt

# 启动服务（开发）
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

启动后：

- Swagger UI：<http://127.0.0.1:8000/docs>
- OpenAPI：<http://127.0.0.1:8000/openapi.json>

## HTTP 接口与请求样例

### 1) 健康检查 / 当前限额

```bash
curl -s http://127.0.0.1:8000/health | python3 -m json.tool
```

### 2) 预检（只校验，不落盘解包）

```bash
# 正常归档
mkdir -p /tmp/demo/dir && echo hello > /tmp/demo/dir/a.txt
tar -cf /tmp/good.tar -C /tmp/demo .

curl -s -F "file=@/tmp/good.tar" \
  http://127.0.0.1:8000/api/v1/inspect | python3 -m json.tool
```

响应包含 `safe: true`、`sha256`、`entry_count`、`kind_counts`、`total_declared_bytes` 与逐条成员信息。

恶意归档（构造一个绝对路径成员）：

```bash
python3 - <<'PY'
import io, tarfile
buf = io.BytesIO()
with tarfile.open(fileobj=buf, mode="w") as tf:
    ti = tarfile.TarInfo("../evil.txt"); payload = b"PWNED"
    ti.size = len(payload)
    tf.addfile(ti, io.BytesIO(payload))
open("/tmp/evil.tar","wb").write(buf.getvalue())
PY

curl -s -F "file=@/tmp/evil.tar" \
  http://127.0.0.1:8000/api/v1/inspect | python3 -m json.tool
# 422 {"error":"unsafe_archive","message":"...traversal...","member":"../evil.txt"}
```

### 3) 隔离解包（校验通过才原子发布）

```bash
curl -s -F "file=@/tmp/good.tar" \
  http://127.0.0.1:8000/api/v1/extract | python3 -m json.tool
```

成功响应：

```json
{
  "sha256": "…",
  "upload_bytes": 10240,
  "extract_id": "ext_6e9f…",
  "path": "/…/.archive_guard_data/extractions/ext_6e9f…",
  "entry_count": 2,
  "written_bytes": 6,
  "entries": [ … ]
}
```

`path` 是服务端发布目录的绝对路径；失败返回 4xx，且该目录不会被创建。

Python 客户端样例：

```python
import requests
with open("/tmp/good.tar", "rb") as fh:
    r = requests.post("http://127.0.0.1:8000/api/v1/extract",
                      files={"file": ("good.tar", fh, "application/x-tar")})
print(r.status_code, r.json())
```

### 错误码

| HTTP | error | 场景 |
|---|---|---|
| 413 | `quota_exceeded` | 上传过大、条目数超限、单文件/总写出字节超限（含 `limit`/`actual`） |
| 422 | `unsafe_archive` | 恶意路径、链接逃逸/环、特殊成员、解压比异常等（含 `member`） |
| 422 | `invalid_archive` | 不是 tar、归档损坏/截断、不支持的压缩格式 |

## 配置（环境变量，均有默认值）

`ARCHIVE_GUARD_HOME`（默认 `./.archive_guard_data`）、`ARCHIVE_GUARD_STORE`、`ARCHIVE_GUARD_SPOOL`，
以及 `AG_MAX_UPLOAD_BYTES`、`AG_MAX_ENTRIES`、`AG_MAX_TOTAL_SIZE`、
`AG_MAX_TOTAL_WRITTEN_BYTES`、`AG_MAX_SINGLE_FILE_BYTES`、`AG_MAX_PATH_DEPTH`、
`AG_MAX_SYMLINK_HOPS` 等。默认值见 `app/archive_guard/limits.py`
（上传 200 MiB、1 万条目、总写出 512 MiB、单文件 128 MiB）。

## 运行测试

```bash
source .venv/bin/activate
python -m pytest -v
```

### 实测结果

**环境**：Ubuntu（Linux 6.8）、Python 3.12.3、依赖按 `requirements.lock` 精确安装。

**自动化测试**（`python -m pytest`）：**44 passed, 0 failed**（约 0.5s）。覆盖：

- 恶意路径：`/etc/passwd`、绝对路径、`../escape`、`foo/../bar/../..`、`a//b`（6 个参数化用例）均在预检阶段拒绝；解包夹具断言目标根外无任何新文件；
- 链接：绝对软链、指向根外的相对软链、链接链逃逸、链接环（hop 上限）、悬空外向链接、硬链接指向缺失/绝对目标，全部拒绝；根内相对软链/悬空根内链接允许；
- 特殊成员：字符设备/块设备/FIFO 拒绝；setuid/setgid/sticky 位写出时被清除（断言磁盘 mode 与 manifest 均无该位）；
- 炸弹/配额：条目数超限、单文件超限、**按实际写出块计数的总配额超限**（`actual=131072 > limit=100000`）、gzip 解压比超限（100 KiB 全零包）、重复成员；
- 损坏：非 tar、bz2（不在支持子集）、负载中途截断（头声明 1024 B、实际不足）均报 `invalid_archive`；
- 失败后目录不发布：多次断言 `store` 目录在失败前后文件集合一致，无 staging 残留、无越界文件。

**真实 HTTP 端到端**（uvicorn 实跑，非 mock）：

| 夹具 | 接口 | 结果 |
|---|---|---|
| 良性 tar（docs/readme.txt） | inspect / extract | `200`，`written_bytes=14`，文件原子发布且内容一致 |
| 良性 gzip tar | inspect | `200`，`compressed=true`、给出压缩后字节与解压比 |
| `../ESCAPED.txt` | inspect / extract | `422 unsafe_archive`（parent-directory traversal） |
| `sub/up -> ../../../../tmp` 后向链接内写文件 | extract | `422`（symlink target escapes archive root） |
| 链接环 `a↔b` 后 `a/pwn` | inspect | `422`（symlink chain exceeds 40 hops） |
| `lnk -> /etc` 绝对链接 | inspect | `422`（absolute symlink target） |
| 字符设备成员 | inspect | `422`（unsupported member type: character device） |
| 150000 字节单文件（配额 100000） | extract | `413 quota_exceeded`，`limit=100000 actual=131072` |

攻击后文件系统核对：`extractions/` 下仅存在良性解包目录；无 `.*.staging.*` 残留；
`/tmp/ESCAPED.txt`、`/ESCAPED.txt`、`/tmp/ag_demo/ESCAPED.txt` 等哨兵路径均不存在；spool 目录已清空。

### 未完成项 / 已知限制

- **仅接受 tar / tar.gz 子集**：不支持 zip、bz2、xz 等（bz2 上传会得到 `invalid_archive`，并有测试固化）。
- **无并发/认证/鉴权/多租户**：本任务要求纯后端安全原语，未加鉴权、限流与上传者隔离；生产部署应放在鉴权网关之后，并对每个调用方单独配置 store/限额。
- **磁盘耗尽的粗粒度防护**：总字节配额按 64 KiB 写出块计数，因此超标量最多比上限多一个块；极端大小的头声明（稀疏/伪造 size）在预检阶段按声明值拒绝。
- **gzip 炸弹的预检解压**：解析 gzip 时 tarfile 会读取解压后的头部流；解压比在读取完成后计算。已用压缩后大小上限（`max_compressed_bytes`）+ 解压比 + 声明总大小三重限制，未做"流式即时比率"中断（属增强项）。
- **解包不保留全部原始元数据**：出于安全，设备/FIFO 拒绝，setuid 等特殊位清除，owner/group 不保留（以运行进程身份落盘）；mtime/xattr/ACL 不还原。
- **无清理/回收策略**：发布目录会一直保留，未实现 TTL 或删除接口。
- 测试依赖（pytest、httpx）也被 `pip freeze` 进了 `requirements.lock`；仅运行服务请安装 `requirements.txt`。
