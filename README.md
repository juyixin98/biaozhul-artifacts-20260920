# archive-guard — 归档解包安全检查服务

纯后端服务（Python + FastAPI + cryptography）：接收 tar 子集归档，先做
**安全预检**，再在**隔离目录**中解包，确认全部条目合法后**原子发布**。

阻止的攻击面：

- **绝对路径**（`/etc/passwd`、盘符路径）
- **目录穿越**（`../../x`、归档名内嵌 `..`）
- **符号链接逃逸**（链接指向绝对路径/根外，经符号链接目录把文件写到根外）
- **符号链接环路 / 长链**、**硬链接逃逸与硬链接环**
- **设备节点 / FIFO 等危险条目类型**、**稀疏文件**
- **解压炸弹**：声明总大小、单文件大小、条目数、压缩比、以及按**实际写出字节**
  计费的总配额（写入过程中超额立即中止）

## 1. 依赖

- Python 3.11+（开发与测试环境：Python 3.12.3，Linux）
- 运行时依赖见 `requirements.txt`（已锁定精确版本）：fastapi、uvicorn、
  python-multipart、cryptography 及其传递依赖
- 测试/示例额外依赖见 `requirements-dev.txt`：pytest、httpx

创建虚拟环境并安装：

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
```

## 2. 启动

```bash
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
```

可选环境变量（括号内为默认值）：

| 变量 | 默认 | 含义 |
| --- | --- | --- |
| `ARCH_DATA_DIR` | `./data` | 隔离区与发布目录的父目录 |
| `ARCH_MAX_UPLOAD_BYTES` | 20 MiB | 单个上传请求体上限（压缩后） |
| `ARCH_MAX_TOTAL_BYTES` | 100 MiB | **实际写出字节**总配额 |
| `ARCH_MAX_FILE_BYTES` | 50 MiB | 单个常规文件上限 |
| `ARCH_MAX_ENTRIES` | 20000 | 条目数上限 |
| `ARCH_MAX_SYMLINK_HOPS` | 16 | 符号链接链最大跳数（环路检测） |
| `ARCH_MAX_COMPRESSION_RATIO` | 100 | 未压缩声明大小 / 归档大小 上限 |

启动后：

- OpenAPI 文档：<http://127.0.0.1:8000/docs>
- 健康检查：`GET /health`

## 3. HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| POST | `/api/v1/archives/precheck` | 只预检，不写任何解包文件 |
| POST | `/api/v1/archives/extract` | 预检 + 隔离解包 + 原子发布 |
| GET | `/api/v1/extractions/{id}` | 查询某次成功解包的元数据清单 |
| POST | `/api/v1/keys/ed25519/generate` | 生成测试用 Ed25519 密钥对 |

上传字段名固定为 `archive`，支持 plain tar / gzip / bzip2 / xz（魔数自动探测）。

### 3.1 可选的 Ed25519 验签

请求携带 `X-Signature`（对**原始上传字节**的 Ed25519 签名，hex）时，必须同时
携带 `X-Public-Key`（PEM 或 DER hex）。验签失败返回 `401`。不传签名头则不校验。

### 3.2 curl 样例

```bash
# 健康检查
curl -s http://127.0.0.1:8000/health

# 构造正常归档并预检
mkdir -p /tmp/demo/docs && echo hello > /tmp/demo/docs/a.txt
tar -cf /tmp/demo/good.tar -C /tmp/demo docs
curl -s -F "archive=@/tmp/demo/good.tar" \
  http://127.0.0.1:8000/api/v1/archives/precheck

# 真正解包（成功后返回 extraction_id）
curl -s -F "archive=@/tmp/demo/good.tar" \
  http://127.0.0.1:8000/api/v1/archives/extract

# 恶意归档：绝对路径 + 目录穿越（应 422，被拦截）
python - <<'PY'
import tarfile, io
buf = io.BytesIO()
with tarfile.open(fileobj=buf, mode="w") as t:
    for n in ("/tmp/abs-evil", "../../rel-evil"):
        m = tarfile.TarInfo(n); m.size = 4
        t.addfile(m, io.BytesIO(b"evil"))
open("/tmp/demo/evil-paths.tar","wb").write(buf.getvalue())
PY
curl -s -F "archive=@/tmp/demo/evil-paths.tar" \
  http://127.0.0.1:8000/api/v1/archives/precheck

# 恶意归档：符号链接逃逸（应 422，link_escape）
python - <<'PY'
import tarfile, io
buf = io.BytesIO()
with tarfile.open(fileobj=buf, mode="w") as t:
    m = tarfile.TarInfo("escape"); m.type = tarfile.SYMTYPE; m.linkname="../../../etc"
    t.addfile(m)
    p = tarfile.TarInfo("escape/pwned"); p.size = 3
    t.addfile(p, io.BytesIO(b"pwn"))
open("/tmp/demo/evil-links.tar","wb").write(buf.getvalue())
PY
curl -s -F "archive=@/tmp/demo/evil-links.tar" \
  http://127.0.0.1:8000/api/v1/archives/extract
```

仓库内还带了 Python 示例客户端（需要 `requirements-dev.txt` 里的 httpx）：

```bash
.venv/bin/python examples/client.py health
.venv/bin/python examples/client.py benign
.venv/bin/python examples/client.py evil-paths
.venv/bin/python examples/client.py evil-links
.venv/bin/python examples/client.py bomb
```

### 3.3 错误码

HTTP `422`（安全拒绝，`error.code`）：`absolute_path`、`path_traversal`、
`link_escape`、`symlink_loop`、`bad_link`、`unsupported_entry_type`、
`sparse_entry`、`file_too_large`、`quota_exceeded`、`too_many_entries`、
`compression_bomb`、`duplicate_entry`、`nul_in_name`、`empty_upload` 等。
上传超体积为 `413 upload_too_large`；归档损坏/格式不识别为
`400 unreadable_archive`；验签问题为 `401`。

## 4. 安全模型（实现要点）

核心代码在 `app/safe_tar.py`，**完全不调用** `TarFile.extract/extractall`。

1. **两阶段规划**：第一遍遍历所有 tar 条目，在纯内存的“虚拟文件系统”中规范化
   路径并逐组件解析符号链接（含跳数上限、环路检测、根外拒绝）；第二遍解析硬链接
   链；第三遍计算每个条目的实际落点并检测落点冲突。任何一条不通过，磁盘上什么
   都不写。
2. **隔离 + 原子发布**：解包在 `data/extracted/<id>/` 下创建
   `.staging-XXXX` 暂存目录；全部文件写出、最终复核通过后，才用一次
   `os.rename` 改名为正式目录。任何异常都只删除暂存目录，**正式目录绝不会以
   半成品状态出现**。
3. **配额按实际写出字节**：文件数据分块（1 MiB）读取写入，每块累加
   `bytes_written` 并与总配额/单文件上限比较；tar 头里声明的 size 仅用于预检，
   不可信。另用压缩比阈值拦截高压缩比炸弹。
4. **纵深防御**：
   - 规划阶段：虚拟解析；
   - 写出阶段：每条路径 `os.path.realpath` 包含关系校验（符号链接建立后立即
     校验，文件写完后再比对落点）；
   - 发布前：对整棵暂存树做最终 realpath 复核。
5. **最小权限落盘**：文件强制 `0600`、目录 `0700`，忽略归档声明的属主、
   setuid/setgid/sticky 位。
6. **隔离区**：上传字节先写入 `data/quarantine/incoming-*` 临时文件（流式
   校验上传体积），处理结束（成功或失败）立即删除。

## 5. 自动化测试

```bash
.venv/bin/python -m pytest
```

`tests/` 共 44 个用例，覆盖：

- 恶意路径（绝对路径、`..` 穿越、内嵌 `..`、NUL/控制字符）；
- 符号链接（绝对目标、根外目标、自环/互环/长链、经符号目录写文件、合法悬空链接、
  合法目录别名）；
- 硬链接（根外目标、环、指向非文件）；
- 危险类型（字符设备、FIFO）、setuid 位被剥离；
- 炸弹（声明超额、单文件超限、条目超限、gzip 高压缩比）；
- **目标根外无写入**、**失败后无半成品目录与暂存残留**、隔离文件清理；
- HTTP 层（成功/拒绝/验签/上传限制/结果查询）端到端。

## 6. 目录结构

```
app/
  config.py      # 环境变量配置
  crypto.py      # cryptography Ed25519 签名/验签/密钥生成
  errors.py      # UnsafeArchiveError / SignatureError
  safe_tar.py    # 核心：规划、虚拟FS、隔离解包、原子发布
  storage.py     # 隔离区落盘、压缩探测、预检/解包编排
  main.py        # FastAPI 路由
tests/           # pytest 套件
examples/client.py
requirements.txt / requirements-dev.txt
```
