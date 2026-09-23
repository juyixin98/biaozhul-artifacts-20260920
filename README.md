# keyvault — 密钥版本审计服务（纯后端）

本项目是一个**本地运行**的安全数据处理服务，实现密钥版本的全生命周期管理与审计：

- 密钥版本状态机：**生成（GENERATED）→ 激活（ACTIVE）→ 停用（DEACTIVATED）→ 销毁（DESTROYED）**
- **加密仅允许使用激活版本**；解密允许激活与已停用版本（历史版本权限）
- **销毁后密钥材料被安全擦除，对应密文明确不可恢复**
- 全部操作写入**带 SHA-256 哈希链的仅追加审计日志**，日志不含密钥材料与明文
- 所有持久化写入均为原子替换（temp + fsync + rename），**崩溃后状态一致**

## 安全边界声明

- 密码原语全部来自成熟库 [`cryptography`](https://cryptography.io/)：**AES-256-GCM**（认证加密），**未自研任何加密算法**
- 密钥由本机 CSPRNG（`secrets.token_bytes(32)`）生成，nonce 12 字节随机生成
- 纯本地服务，**不连接任何生产账号 / 外部系统**；测试密钥全部本地生成
- HTTP 层使用 Python 标准库 `http.server`，仅监听 `127.0.0.1`，无前端

## 目录结构

```
keyvault/
  models.py    # 状态机定义（合法迁移表、解密权限）
  store.py     # 持久化：原子写、密钥材料安全擦除、崩溃一致性自检
  audit.py     # 审计日志：JSONL 仅追加 + SHA-256 哈希链
  service.py   # 核心服务：状态机迁移、AES-256-GCM 加解密、审计埋点
  server.py    # HTTP JSON API（标准库 http.server）
tests/         # 40 个自动化测试（pytest）
examples/requests.sh  # 请求样例脚本（curl）
```

## 安装与运行

```bash
pip install -r requirements.txt   # 依赖：cryptography、pytest

# 启动服务（数据目录 ./keyvault-data，监听 127.0.0.1:8765）
python3 -m keyvault.server --data-dir ./keyvault-data --port 8765

# 运行请求样例（另开一个终端）
bash examples/requests.sh

# 运行自动化测试
python3 -m pytest tests/ -v
```

## 状态机与权限规则

| 当前状态 | 允许的迁移 | 可加密 | 可解密 |
|---|---|---|---|
| GENERATED | → ACTIVE, → DESTROYED | ❌ | ❌ |
| ACTIVE | → DEACTIVATED | ✅（唯一可加密状态） | ✅ |
| DEACTIVATED | → ACTIVE, → DESTROYED | ❌ | ✅（历史版本权限） |
| DESTROYED | （终态，不可逆） | ❌ | ❌（明确不可恢复） |

- 任意时刻至多一个 ACTIVE 版本；激活新版本时旧版本自动转为 DEACTIVATED
- 销毁 ACTIVE 版本会被拒绝（HTTP 409），须先停用
- 销毁 = 随机数据覆写密钥文件 + fsync + 删除，元数据保留为墓碑供审计

## HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/keys` | 生成新版本 |
| GET | `/keys` | 列出所有版本元数据 |
| GET | `/keys/{vid}` | 查询单个版本 |
| POST | `/keys/{vid}/activate` | 激活（旧激活版本自动停用） |
| POST | `/keys/{vid}/deactivate` | 停用 |
| POST | `/keys/{vid}/destroy` | 销毁（不可逆） |
| POST | `/encrypt` | `{"plaintext_b64": "..."}` → `{"envelope": {...}}` |
| POST | `/decrypt` | `{"envelope": {...}, "expect_version": "v1"?}` → 明文 |
| GET | `/status` | 服务状态 + 崩溃一致性自检 |
| GET | `/audit` | 审计日志全部记录 |
| GET | `/audit/verify` | 校验审计哈希链 |

错误码：`404` 版本不存在 / `409` 非法状态迁移或无激活版本 / `410` 密钥已销毁 / `422` 版本不符或密文校验失败。

密文信封格式：`{"v": "v1", "nonce": "<base64>", "ct": "<base64>"}`（AES-256-GCM，tag 附在 ct 尾部）。

## 验收标准与测试对照

| 验收项 | 测试 |
|---|---|
| 交错轮换与请求 | `tests/test_rotation_interleaved.py`（含 200 步随机交错工作负载 + 终态不变量校验） |
| 错误版本拒绝 | `tests/test_crypto.py::test_wrong_version_rejected`、`test_tampered_version_tag_rejected` |
| 审计无密钥泄露 | `tests/test_audit.py::test_audit_contains_no_key_material`、`test_audit_contains_no_plaintext` |
| 崩溃后状态一致 | `tests/test_crash_recovery.py`（含对真实服务进程 `kill -9` 后重启校验） |
| 销毁不可恢复 | `tests/test_crypto.py::test_destroyed_key_cannot_decrypt`、`test_crash_recovery.py::test_destroy_survives_restart` |
| 审计链防篡改 | `tests/test_audit.py::test_audit_tamper_detected`、`test_audit_truncation_detected` |

## 实际运行记录（2026-09-24，本机 Ubuntu / Python 3.12.3 / cryptography 41.0.7 / pytest 9.1.1）

### 1. 自动化测试

```
$ python3 -m pytest tests/ -q
........................................                                 [100%]
40 passed in 1.74s
```

**40 个测试全部通过，无未通过项。**（开发过程中曾有 1 次失败：
`test_audit_records_all_operations` 因测试自身漏写 decrypt 调用而断言失败，
属测试用例笔误，修正后通过；非产品代码缺陷。）

### 2. 请求样例实跑（`bash examples/requests.sh`，实机输出节选）

```
=== 3. 用激活版本加密 ===
{ "v": "v1", "nonce": "sL2ed8m+s4NNV5sh", "ct": "fqzC12dSYh71q0bfHfk+LxNesTZXOxXe53UICv2Y" }

=== 6. 历史密文仍可用 v1 解密（轮换到 v2 后）===
{ "plaintext_b64": "aGVsbG8ga2V5dmF1bHQ=" }

=== 7. 错误版本拒绝 ===
HTTP 422
{ "error": "WrongVersionError", "message": "密文由 v1 加密，与指定版本 v2 不符" }

=== 9. 销毁后密文明确不可恢复 ===
HTTP 410
{ "error": "KeyDestroyedError", "message": "版本 v1 已销毁，密文不可恢复" }

=== 10. 非法迁移（重复销毁）===
HTTP 409

=== 12. 审计日志哈希链校验 ===
{ "ok": true, "reason": null }
```

完整输出共 13 步全部符合预期。审计日志中仅含版本号与 SHA-256 摘要
（如 `"plaintext_sha256": "83d4..."`），无任何密钥材料或明文。

### 3. 崩溃恢复实机验证

对运行中的服务进程发送 `kill -9`（模拟断电），随后以同一数据目录重启：

```
--- 崩溃重启后 /status ---
active: v2 | consistency: [] | states: {'v1': 'DESTROYED', 'v2': 'ACTIVE'}
--- 崩溃重启后 /audit/verify ---
{"ok": true, "reason": null}
--- 重启后直接加密（验证服务可用）---
{"envelope": {"v": "v2", "nonce": "HhPosZj4CdDOLOwU", "ct": "xuJZYPCKA71o4EO7Etno8n2aSVx7CKhyFdMb"}}
```

崩溃前销毁的 v1 保持 DESTROYED 且密钥文件不存在、v2 保持 ACTIVE、
审计链完整、服务可继续正常加密——状态一致。

## 已知限制

- 密钥材料以本地文件（0600 权限）存放，未使用 HSM/KMS；适用于本地与测试场景
- 审计日志防篡改依赖哈希链，攻击者若可整体重写日志文件则无法检测（需配合外部锚定/WORM 存储）
- HTTP 服务未做认证与 TLS，仅绑定回环地址，面向本机调用
