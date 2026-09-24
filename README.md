# 信封加密数据轮换服务 (Envelope Encryption with Master-Key Rotation)

纯后端 Python 服务: 对文件块做**信封加密** (envelope encryption), 每对象独立数据密钥 (DEK),
使用成熟 AEAD (AES-256-GCM) 与唯一 nonce; **主密钥轮换只重新包裹 DEK**, 数据密文块不动;
容器头部版本号纳入认证数据 (AAD), 头部任何字段被篡改都会导致认证失败。

无界面, 仅提供 HTTP (FastAPI) 接口。内存存储, 进程重启后数据清空。

---

## 1. 依赖与启动

要求 Python 3.10+ (开发 / 验证环境: Python 3.12.3, Linux)。

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt          # 安装锁定依赖 (cryptography / fastapi / uvicorn / pytest / httpx)

uvicorn app.main:app --host 127.0.0.1 --port 8000
# API 文档(Swagger UI): http://127.0.0.1:8000/docs
```

### 直接依赖 (`requirements.in`)

| 包 | 用途 |
| --- | --- |
| `cryptography` | AES-256-GCM (密钥包裹 + 数据分块 AEAD) |
| `fastapi` + `uvicorn[standard]` | HTTP 接口 |
| `pytest` + `httpx` | 自动化测试 / 测试客户端 / demo 脚本 |

完整可复现的传递依赖版本 (含哈希上游版本号) 锁定在 `requirements.txt`
(`pip freeze` 生成, 本次验证环境安装成功)。

---

## 2. 运行测试与演示

```bash
# 自动化测试: 48 个用例 (加密原语 / 轮换服务 / HTTP 端到端)
python -m pytest

# 一键端到端演示 (自动起 uvicorn 子进程, 跑完自动停止):
python demo.py --port 8765
```

---

## 3. 容器格式

多字节整数一律大端。一个对象 = 一个自描述信封字节串:

```
magic         4B    b"ENV1"
version       1B    0x01                      ┐
key_id_len    1B                              │
key_id        key_id_len B  (ASCII, 如 "v2")  │ 头部 (明文, 但字段全部被 AEAD 认证)
salt          8B    每对象随机                │
block_size    4B                              │
pt_len        8B    明文总长度                │
wrapped_len   2B                              │
wrapped_dek   wrapped_len B                   ┘  = wrap_nonce(12B) + AES-256-GCM(KEK, DEK)
blocks        N 个 AES-256-GCM 输出拼接 (密文 + 16B tag)
```

关键设计:

* **每对象独立 DEK** (`AESGCM.generate_key(256)`); 主密钥 (KEK) 永不接触数据。
* **DEK 包裹**: `AES-256-GCM(KEK, nonce=随机12B, plaintext=DEK, AAD=
  "env1-dek-wrap" || version || key_id || salt || block_size || pt_len)`。
  → version / key_id / salt / 长度字段全部在 AAD 中, 改头部任一字节 → DEK 解包 `InvalidTag`。
* **数据块加密**: 块大小可配 (默认 64 KiB);
  nonce = `salt(8B) || block_index(4B 大端)` —— salt 每对象随机, index 块间递增,
  同一 DEK 下 nonce 全局唯一, 无需计数器持久化;
  块 AAD = `"env1-blk" || version || block_index || pt_len` —— 防块调换 / 块拼接 / 长度篡改。
* **空对象** 生成 0 个块, DEK 仍被正常包裹。
* **主密钥轮换** (`app/crypto.py::rewrap_header`): 只解包 DEK 再用新 KEK 重新包裹,
  重写头部; salt / nonce / 数据块字节全部原样保留。
* **解密不返回部分明文**: `decrypt_blocks` 先逐块写入临时缓冲, 全部块 GCM 校验通过后
  才整体返回; 任一块失败立即抛异常。接口层失败统一 4xx JSON, 不带任何明文字节。

---

## 4. HTTP 接口与请求样例

### 4.1 上传对象 (加密保存)

两种请求体任选:

```bash
# (a) 原始字节 (任意二进制, 推荐)
curl -X PUT --data-binary @file.bin \
  -H "Content-Type: application/octet-stream" \
  "http://127.0.0.1:8000/objects/file.bin?block_size=4096"

# (b) JSON + base64 (方便纯文本 curl / 浏览器)
curl -X PUT http://127.0.0.1:8000/objects/hello \
  -H "Content-Type: application/json" \
  -d '{"plaintext_b64":"aGVsbG8gZW52ZWxvcGU=", "block_size": 4096}'
```

`201` 响应含元数据 (头部字段), 例:

```json
{"name":"demo.txt","created_at":"2026-09-24T02:14:53+00:00","updated_at":"...",
 "metadata":{"name":"demo.txt","version":1,"key_id":"v1","block_size":4,
             "plaintext_len":14,"salt_hex":"d8b3534dd64b237c", ...}}
```

### 4.2 下载对象 (解密)

```bash
curl http://127.0.0.1:8000/objects/demo.txt        # 200 application/octet-stream, 明文
curl http://127.0.0.1:8000/objects/demo.txt/metadata
curl http://127.0.0.1:8000/objects
```

认证失败 → `400 {"error":"decrypt_failed"|"format_error", ...}`;
密钥缺失 → `409 {"error":"key_unavailable", ...}`; 对象不存在 → `404`。

### 4.3 主密钥与轮换

```bash
curl http://127.0.0.1:8000/keys                 # 列出主密钥版本
curl -X POST http://127.0.0.1:8000/keys/rotate  # 生成新版本并重包裹全部 DEK
# => {"new_key_id":"v2","rewrapped":2,"total_pending":2}

curl -X POST "http://127.0.0.1:8000/keys/rotate?fail_after=2"   # 故障注入: 重包裹 2 个后模拟崩溃
curl -X POST http://127.0.0.1:8000/keys/rewrap                 # 中断后续跑 (幂等收尾)
curl -X DELETE http://127.0.0.1:8000/keys/v1                   # 删除旧版主密钥 (当前密钥不可删)
```

轮换语义: 新版本主密钥先生成并"生效", 再按对象名排序逐个重包裹;
`fail_after=N` 在重包裹 N 个后抛 `500 rotation_interrupted`, 系统进入**混合包裹状态**
(部分对象 v_old, 部分 v_new, 两版密钥都在, 全部仍可解密);
`/keys/rewrap` 把所有未使用当前密钥的对象幂等收尾。

### 4.4 故障注入端点 (仅用于验收 / 安全演示, 不计入正式 schema)

```bash
curl -X POST http://127.0.0.1:8000/objects/<name>/attack/header     # 翻转头部 wrapped DEK 一字节
curl -X POST http://127.0.0.1:8000/objects/<name>/attack/truncate   # 砍掉末块 3 字节
curl -X POST http://127.0.0.1:8000/objects/<name>/attack/bitflip    # 翻转密文末字节
```

端点完成破坏后走正常解密路径, 预期均返回 `400` 且响应体不含明文。

---

## 5. 验收场景与结果

以下场景均有自动化测试覆盖 (`tests/`) 并经真实 HTTP 演示 (`demo.py`) 验证。

| 验收点 | 覆盖位置 | 实测结果 |
| --- | --- | --- |
| 每对象独立 DEK / 唯一 nonce | `test_crypto.py::test_each_object_has_independent_dek`, `test_nonce_unique_per_block_no_nonce_reuse` | PASS |
| 模拟轮换中断 → 混合状态 → 续跑; 旧对象轮换后仍可解密 | `test_store.py::test_rotation_interrupted_leaves_mixed_state_and_resume`, `test_api.py::test_rotation_interrupt_then_resume_via_api`; demo 场景 3/4 | PASS |
| 错误密钥 (删除旧主密钥) → 409, 不返回明文 | `test_crypto.py::test_wrong_master_key_fails`, `test_api.py::test_wrong_key_returns_409_no_plaintext`; demo 场景 5 | PASS |
| 头部篡改 (version / key_id / 长度字段 / wrapped DEK) → 400 | `test_crypto.py::test_header_tamper_*`; demo 场景 6 | PASS |
| 密文截断 (tag 缺字节 / 整块缺失 / 头部截断 / 尾部多余) → 400 | `test_crypto.py::test_ciphertext_truncated_*`, `test_trailing_garbage_fails`; demo 场景 7 | PASS |
| 比特翻转 / 块调换 → 400 | `test_crypto.py::test_ciphertext_bitflip_fails`, `test_block_swap_fails` | PASS |
| 失败不返回部分明文 (损坏只发生在最后一块) | `test_crypto.py::test_no_partial_plaintext_on_failure`, API 侧断言错误响应不含明文 | PASS |
| 轮换只动头部, 密文块字节不变 | `test_store.py::test_rotation_rewraps_only_header_and_old_objects_decrypt` | PASS |

**实际执行记录 (本仓库环境, Python 3.12.3, Ubuntu, 2026-09-24):**

```
python -m pytest          -> 48 passed, 1 warning (starlette TestClient 的 httpx 弃用提示, 不影响功能)
python demo.py --port 8765 -> 全部 23 个检查项 PASS (含真实 uvicorn 上的 curl 等价请求)
```

---

## 6. 项目结构

```
app/
  crypto.py     信封格式 / AES-256-GCM 包裹与分块加解密 / rewrap_header
  store.py      KeyStore (多版本主密钥)、ObjectStore (内存对象存储)、轮换与中断模拟
  main.py       FastAPI 路由、错误映射、故障注入端点
tests/
  test_crypto.py  原语 / 容器格式 / 篡改 / 截断 / 块调换 / rewrap (17 例)
  test_store.py   KeyStore/ObjectStore、轮换、中断续跑、删密钥 (11 例)
  test_api.py     HTTP 端到端 (17 例)
demo.py           真实 HTTP 一键演示
requirements.in   直接依赖清单
requirements.txt  锁定的完整依赖版本
pytest.ini
```

## 7. 已明确的边界与未做事项 (如实记录)

* **存储是内存实现**: `ObjectStore`/`KeyStore` 进程内保存, 重启即清空; 接口已按实例隔离
  (FastAPI 依赖注入), 替换为数据库 / KMS / HSM 后端时只需改 `store.py`。生产环境主密钥
  不应由应用进程生成裸放内存, 应接 KMS/HSM 或至少落 KEK 加密的密钥文件。
* **轮换是同步全量过程**: 对象量很大时应改为分页 / 后台任务 + 进度持久化; 当前
  `fail_after` 只模拟"崩溃点", 没有真正的崩溃恢复日志 (新密钥已建立这一持久化语义靠
  KeyStore 版本号体现, 内存里续跑直接幂等)。
* **无认证 / 鉴权 / 多租户 / 审计日志**: 任务要求纯后端加密能力, 未包含 HTTP 层鉴权。
* **不提供流式上传 / 下载**: 目前整对象缓冲在内存; 容器格式本身支持流式分块处理,
  接口层可基于 `Request.stream()` / `StreamingResponse` 扩展 (解密仍须全部块认证后才能
  对外输出, 可采用"缓冲至认证完成"或后台预认证流水线, 不能边解边吐)。
* **不提供信封容器的公开 CLI / 跨语言 SDK**; 格式文档见第 3 节。
* `attack/*` 端点是为验收/教学加的故障注入入口, 生产部署应关闭或加权限。
