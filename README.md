# 加密范围读取（Encrypted Range Store）

纯后端、纯本地的安全数据处理服务：对象以**固定块 AEAD** 格式加密落盘，
支持 HTTP 风格的**跨块范围读取**；每一块必须先通过认证，其明文才允许返回，
关联数据（AAD）把**长度与位置**绑定进每一块的认证标签。

- 语言：Python 3.10+（实测 3.12.3）
- 密码原语：只用 [`cryptography`](https://cryptography.io/) 的
  AES-256-GCM（`AESGCM`）与 HKDF-SHA256，**不自创任何加密算法**
- 密钥：本地随机生成（`os.urandom`），存于数据目录 `master.key`（权限 0600），
  **不接任何生产账号 / KMS / 云服务**
- Web 层：Python 标准库 `http.server`，无前端、无第三方 Web 框架

## 威胁模型与安全属性

服务端是"诚实但存储可能被篡改"的模型：攻击者可以复制、截断、拼接、
交换密文文件或块。系统保证：

1. **逐块认证后才返回**：范围请求覆盖的所有块先全部通过 AES-GCM 验证，
   再返回明文；任一块失败则整体失败（HTTP 409），不返回部分明文。
2. **长度与位置绑定（AAD）**：每块的 AAD 包含
   魔数/版本、对象 ID、块索引、**该块在文件中的字节偏移**、逻辑块大小、
   **本块明文长度**、对象总明文长度、总块数。
   因此：块重排、块移动到不同偏移、跨对象的密文交换（密文交换）、
   篡改块长度字段都会在认证阶段失败。
3. **清单认证**：对象元数据（对象 ID、块大小、总长度、块数）本身经
   AES-GCM 加密认证，打开对象时即验证；清单中的 id 与请求 id 不符直接拒绝。
4. **结构完整性**：打开时遍历块头，检测文件截断；读取后检测未认证的
   尾部字节（检测拼接 / 扩展）。
5. 每块独立随机 96-bit nonce；每对象随机 16 字节盐，经 HKDF 分别派生
   清单密钥与块密钥。

非目标：不提供多用户访问控制（本地服务，可选单一 Bearer token）、
不做密钥轮换、不隐藏对象大小。

## 文件格式 `ENCRRNG1`

```
魔数 8B "ENCRRNG1" | 版本 1B (=1) | 盐长度 1B | 盐(16B)
| 清单密文长度 8B | 清单 nonce 12B | 清单密文(含16B GCM标签)
| 数据块 × N：
    nonce 12B | 密文长度 4B | 密文(明文 + 16B GCM标签)
```

清单明文为 JSON：`object_id / block_size / plaintext_len / n_blocks`。
默认逻辑块大小 64 KiB，最后一块为短块；空对象 0 个数据块。

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/healthz` | 健康检查（免鉴权） |
| `PUT` | `/objects/{id}` | 上传明文，服务端加密落盘；需 `Content-Length` |
| `GET` | `/objects/{id}` | 整对象读取（200）或带 `Range` 的范围读取（206） |
| `DELETE` | `/objects/{id}` | 删除（204） |

- 对象 ID 白名单：`[A-Za-z0-9_-]{1,128}`，杜绝路径穿越。
- 范围：单区间 `bytes=start-end`、`bytes=start-`、`bytes=-suffix`；
  多区间返回 400。终点超过对象长度时截断；起点越界返回 416。
- 状态码：400 语法/ID 错误，401 未认证，404 不存在，409 完整性失败，
  411 缺 Content-Length，413 超过 16 MiB 上传上限，416 范围无法满足。
- 完整性失败的响应体是 JSON 错误信息，**不含任何对象明文**。

## 快速开始

```bash
pip install -r requirements.txt

# 启动（仅监听 127.0.0.1；主密钥缺失时自动生成到 ./data/master.key）
python -m encrypted_range_store --data-dir ./data serve --port 8080

# 可选：启用 Bearer 认证
ERS_TOKEN=$(openssl rand -hex 16) python -m encrypted_range_store \
  --data-dir ./data serve --port 8080
```

请求样例见 [`examples/requests.sh`](examples/requests.sh)
与 [`examples/requests.py`](examples/requests.py)。

```bash
# 上传
curl -s -X PUT --data-binary @file.bin http://127.0.0.1:8080/objects/file
# 首 / 尾 / 跨块范围
curl -s -H 'Range: bytes=0-99'     http://127.0.0.1:8080/objects/file
curl -s -H 'Range: bytes=-100'     http://127.0.0.1:8080/objects/file
curl -s -H 'Range: bytes=60-140'   http://127.0.0.1:8080/objects/file
```

也可以不走 HTTP，直接用 CLI：

```bash
python -m encrypted_range_store --data-dir ./data put local.bin myobj
python -m encrypted_range_store --data-dir ./data get myobj --range 10-20
```

Python API：

```python
from encrypted_range_store import EncryptedObjectStore, generate_master_key

store = EncryptedObjectStore("./data", master_key=generate_master_key())
store.put("o", b"hello" * 100000)
store.get_range("o", 100, 200)          # 跨块范围，逐块认证后返回
```

## 测试

```bash
python -m pytest -q
```

测试覆盖（64 个用例，全部自动化）：

- 首/尾范围、跨多块范围、逐字节边界穷举、最后短块、空对象
- 密文翻转（块密文 / nonce / 清单）、文件截断、尾部拼接、魔数破坏
- 错误主密钥
- **密文交换**：跨对象整文件交换、跨对象同位置块交换、同文件内块交换、
  同内容块移动到不同偏移——全部 409
- HTTP 端到端：200/206/400/401/404/409/411/416 与 `Content-Range`
- 篡改响应中不泄漏任何明文片段（断言）

## 实际运行记录

以下为 2026-09-24 在本机（Linux 6.8 / Python 3.12.3 / cryptography 41.0.7 /
pytest 9.1.1）的真实执行结果。

### 自动化测试

```text
$ python3 -m pytest -q
................................................................         [100%]
64 passed in 6.26s
```

无未通过项、无跳过项。

### 手工端到端（真实起服务 + curl，块大小分别为默认 64KiB 与 64B）

- PUT 200 字节对象 → `201 {"object_id":"demo","size":200}`；PUT 空对象 → 201。
- GET 整对象与源文件 `sha256` 完全一致：
  `b531abd8dae7232c861ac9f50aff9952d29c8d4c3772551cc5bce5d39d2cd08d`。
- 首范围 `bytes=0-9` → 206，`Content-Range: bytes 0-9/200`，正文 10 字节正确。
- 尾范围 `bytes=195-199` 与 `bytes=-5` → 206，正文 `555c 636a 71` 正确。
- 跨块范围 `bytes=60-140`（块 64B）→ 206，81 字节与明文切片逐字节相等。
- 空对象 GET → 200、`Content-Length: 0`；空对象 Range → 416。
- 64B 块下 `0-0 / 0-63 / 63-64 / 128-199 / 199-199 / 60-140`
  六个边界范围的状态码、`Content-Range` 与正文全部断言通过
  （200 = 3 个满块 + 8 字节短块）。
- 落盘文件除魔数/版本/长度外无明文：200 字节明文对象落盘 366 字节，
  扫描不到任何 8 字节明文片段。
- **篡改**：原地翻转数据块密文区一个字节 → 整读、首范围、尾范围全部
  `409 {"error":"第 0 块 AEAD 认证失败"}`，响应体中扫描不到明文片段；
  重新 PUT 后恢复正常。
- **密文交换**：把对象 B 的整份密文复制到 A 的路径 → 读取 A 返回
  `409 {"error":"清单绑定的对象标识与请求对象不符（检测到密文交换）"}`，
  B 本身仍 200 可读。
- 错误 Range：`bytes=200-`→416，`bytes=x-y`→400，`items=0-1`→400，
  多区间→400。
- Bearer：无 token/错 token → 401；正确 token → 正常；`/healthz` 免鉴权。
- CLI `put`/`get --range` 正常，整读与源文件 `cmp` 一致；
  `master.key` 权限为 `-rw-------`。

以上手工命令可直接通过 `examples/requests.sh` 复现（脚本自行选择空闲端口）。

## 目录结构

```
encrypted_range_store/
  __init__.py      公共 API
  errors.py        异常（IntegrityError 等）
  format.py        ENCRRNG1 格式：加密写、认证读、范围切片
  service.py       对象存储、密钥保管、HTTP Range 解析
  server.py        标准库 HTTP 服务
  __main__.py      CLI（serve / put / get）
tests/             pytest 用例（格式层 / 服务层 / HTTP 端到端）
examples/          curl 与 Python 请求样例
requirements.txt
```
