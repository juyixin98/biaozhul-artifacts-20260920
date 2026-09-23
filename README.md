# 信封加密 + 主密钥轮换（本地安全数据处理服务）

纯后端 Python 项目：对本地文件做**分块 AEAD 信封加密**，数据密钥（DEK）由主密钥包裹；
**轮换主密钥时只重新包裹 DEK，绝不重新加密数据块**。

- 密码原语：仅使用 [`cryptography`](https://cryptography.io/) 提供的 **AES-256-GCM**，不自创任何算法；
- 所有密钥均为**本地随机生成**，不接入任何生产账号 / KMS；
- 提供库 API、命令行、仅监听 `127.0.0.1` 的演示 HTTP 接口；
- 不做前端。

> ⚠️ 本项目仅用于本地数据处理、演示与测试。主密钥以明文 JSON（0600 权限）存在本机，
> 生产环境应把主密钥放在 HSM/KMS 中。不要把它部署成生产服务。

---

## 1. 它解决什么问题

经典信封加密：

```
主密钥 MK（密钥环中，按 kid 管理；可多代）
   └── AES-GCM 包裹 ── 数据密钥 DEK（每文件随机一把，密文随文件头存放）
                          └── AES-GCM 分块加密 ── 文件明文
```

主密钥轮换时，新主密钥只需把文件头部那 32 字节 DEK 重新包裹一次，
GB 级密文数据**逐字节不动**，所以轮换很快，也不存在"轮换期间重写数据"的大窗口。

## 2. 安全设计（与验收点一一对应）

| 威胁 / 要求 | 设计 |
|---|---|
| 块被篡改 | 每块独立 AES-GCM 认证标签，解密逐块校验，失败即中止 |
| 把 A 文件的块换到 B 文件 | 每块 AAD 绑定 `file_id` |
| 同文件内调换/重放块 | 每块 AAD 绑定 `chunk_index`（0 起） |
| 块长度被改动 | 每块 AAD 绑定该块**明文长度** |
| 文件被截断 | 头部声明 `plaintext_size`/`chunk_count`，按精确长度读取；短读即 `TruncatedContainer`；尾部多余字节也拒绝 |
| 头部字段被篡改 | 头部所有解密语义字段进入头部 AAD，由 DEK 的 GCM 标签认证 |
| 把 DEK 包裹密文挪到别的文件/用途 | 包裹 AAD 绑定 `kid` 与用途标签 `envenc/v1/wrap`，头部 AAD 再次绑定 kid |
| 魔数/版本伪造 | 显式校验 `ENVENC` magic 与版本号 |
| **nonce 唯一性** | 见下节 |
| **明文临时文件** | 见下节 |
| 主密钥轮换中断 | 临时文件 + `fsync` + `os.replace` 原子替换；中断后原文件不动、临时文件清理；操作幂等可重入 |

### 2.1 nonce 唯一性（重点说明）

AES-GCM 的安全底线是：**同一把密钥下 nonce 绝不复用**。本项目的保证：

1. **每文件一把随机 DEK**（32 字节，OS CSPRNG）。即使两个文件的块 nonce 字节恰好相同，
   它们也在不同密钥作用域，不构成复用；
2. 文件内块 nonce = **4 字节随机前缀（每文件一个，存文件头）+ 8 字节大端块计数器**（0,1,2,…），
   结构上天然唯一；
3. DEK 包裹、头部认证各使用一把一次性 12 字节随机 nonce，分别处于"主密钥""该文件 DEK"作用域；
4. 块数被限制在 2³² 以内（远小于 8 字节计数器上限）。

### 2.2 明文临时文件处理

- **加密**：源文件只读流式读取，密文直接写目标路径的 `.enc.tmp`（0600），
  fsync 后 `os.replace` 改名——全过程**没有任何明文临时文件**；
- **解密**：先写目标路径旁的 `.part`（0600），**所有块都认证通过后**才原子改名为目标明文；
  任何认证失败/截断都会删除 `.part`，磁盘上不会留下半截明文。
- HTTP 接口的数据只在内存（base64/JSON）处理，同样不落明文临时文件。

### 2.3 容器格式（ENVENC v1，大端）

```
偏移 0   magic "ENVENC" (6B) | version 0x01 (1B) | HEADER_LEN (uint32, 4B)
偏移 11  HEADER_JSON（规范 JSON：sort_keys、无空白；字段见源码 container.py 模块文档）
之后     块序列，每块 = NONCE(12B) || AES-GCM 密文（明文块 + 16B 标签）
```

头部字段：`v, file_id, kid, chunk_size, plaintext_size, chunk_count,
nonce_prefix_b64, wrap_nonce_b64, wrapped_dek_b64, hdr_nonce_b64, hdr_tag_b64`。

## 3. 安装

需要 Python 3.10+（实测 3.12.3）。

```bash
python3 -m venv .venv && . .venv/bin/activate    # 可选
pip install -r requirements.txt                  # cryptography>=41
```

## 4. 命令行用法

```bash
# 初始化本地密钥环（0600 权限 JSON，含第一把 active 主密钥）
python -m envelope_enc.cli keyring-init -k keys.json

# 加密 / 查看头部 / 解密
python -m envelope_enc.cli encrypt -k keys.json -i secret.bin -o secret.bin.enc -c 65536
python -m envelope_enc.cli inspect secret.bin.enc          # 不需要密钥环
python -m envelope_enc.cli decrypt -k keys.json -i secret.bin.enc -o secret.out

# 主密钥轮换：新主密钥 active，旧主密钥 retired（仍可解旧文件）
python -m envelope_enc.cli rotate-master -k keys.json
python -m envelope_enc.cli keyring-ls -k keys.json

# 把已有密文文件批量重包到当前 active 主密钥（幂等，可重复执行）
python -m envelope_enc.cli rotate -k keys.json secret.bin.enc another.enc

# 确认全部文件迁移完后，可删除 retired 旧主密钥（删除后旧文件永久不可解）
python -m envelope_enc.cli delete-key -k keys.json mk-0001-xxxxxxxxxx
```

## 5. HTTP 演示接口

仅绑定回环地址，静态 Bearer Token：

```bash
python -m envelope_enc.server -k keys.json --token s3cret --port 8080
# 默认 127.0.0.1；显式绑定非回环地址会打印警告
```

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| GET | `/health` | 否 | 健康检查 |
| GET | `/keys` | 是 | 列出主密钥与 active_kid |
| POST | `/keys/rotate-master` | 是 | 轮换主密钥 |
| POST | `/encrypt` | 是 | `{"data_b64": "...", "chunk_size": 65536?}` |
| POST | `/decrypt` | 是 | `{"ciphertext_b64": "..."}` |
| POST | `/rotate` | 是 | `{"ciphertext_b64": "..."}` |

完整 curl 与 JSON 样例见 [`examples/requests.md`](examples/requests.md)。
请求体上限 10 MiB。

## 6. 库 API 速览

```python
from envelope_enc import Keyring, encrypt_bytes, decrypt_bytes, rotate_bytes

kr = Keyring("keys.json"); kr.initialize(exist_ok=True)
blob = encrypt_bytes(b"hello", kr)
assert decrypt_bytes(blob, kr) == b"hello"

kr.rotate_master()                 # MK2 active，MK1 retired
blob2 = rotate_bytes(blob, kr)     # 只重包 DEK；blob2 的密文块与 blob 逐字节相同
assert decrypt_bytes(blob2, kr) == b"hello"
```

大文件用流式文件版：`encrypt_file / decrypt_file / rotate_file / rotate_many`。

## 7. 自动化测试

```bash
python -m pytest -q
```

67 个用例，覆盖：底层 AEAD 与 nonce、空文件与分块边界、块篡改、跨文件/文件内换块、
换 nonce、头部各字段篡改、包裹 DEK 互换、魔数/版本、截断（头/块/空文件/磁盘文件）、
尾部多余字节、错误密钥环、轮换只重包 DEK（块区哈希不变）、轮换幂等、连续多代轮换、
**轮换中断（临时文件写好后崩溃）后原文件完好且可重入**、批量轮换部分跳过、
CLI 端到端（subprocess）、HTTP 业务函数与真实 socket（含 401/404）。

## 8. 实测记录

见 [`RUNLOG.md`](RUNLOG.md)：在本机实际执行的命令、输出与结论，包括篡改块、
交换文件块、块换位、截断、轮换中断的真实拒绝/恢复结果，以及 `pytest` 结果。
未使用任何生产账号，未发明加密算法。

## 9. 目录结构

```
envelope_enc/
  crypto.py     # AES-256-GCM 封装、nonce 拼装、各上下文 AAD 编码
  keyring.py    # 本地主密钥环（JSON，0600，原子写）
  container.py  # ENVENC v1 容器：分块加密/解密/只重包 DEK 的轮换
  cli.py        # 命令行
  server.py     # 127.0.0.1 演示 HTTP 服务
tests/          # pytest 套件
examples/       # HTTP/curl 请求样例
RUNLOG.md       # 实际运行记录
requirements.txt
```
