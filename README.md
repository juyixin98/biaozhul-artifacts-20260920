# 签名制品清单（Signed Artifact Manifest）— 纯后端

本地、离线的**制品签名与验证服务**：对一个目录（制品）生成全部文件的 SHA-256
摘要清单，用 Ed25519 私钥签名；验证方依据本地信任库（可信公钥集合）校验
**内容摘要、签名、可信密钥 ID**，全部通过后才允许执行制品入口。

- 语言：Python 3.9+（开发与实测环境 Python 3.12.3）
- 密码学：仅使用成熟库 [`cryptography`](https://cryptography.io/)（Ed25519 /
  SHA-256 / PKCS#8 PEM），**不自创任何加密算法或原语**
- 测试密钥全部本地生成，**不接任何生产账号/服务**，全程离线可用
- 无前端；提供 Python API、CLI、仅监听 loopback 的 HTTP 服务

---

## 1. 它解决什么问题

假设你要在机器上运行一个制品目录（脚本 + 数据 + 配置）。你需要确认：

1. 清单确实由你信任的密钥签名（**签名 + 可信密钥 ID**）；
2. 磁盘上每个文件与签名时逐字节一致（**SHA-256 摘要 + 大小**）；
3. 清单没有引用制品目录之外的路径（**防路径逃逸**）；
4. 签名的字节表示不因 JSON 字段顺序/排版差异而失效（**规范化 JSON**）；
5. 任何一项不通过，**制品入口绝不被执行**。

---

## 2. 目录结构

```
sig_manifest/
  canonjson.py   规范化 JSON（确定性序列化、重复键拒绝、NFC、键排序）
  paths.py       制品路径安全（词法校验 + realpath 包含校验，防穿越/符号链接逃逸）
  keys.py        Ed25519 密钥、密钥 ID、PEM 读写、信任库
  manifest.py    制品枚举/摘要、signed 构造、签名、信封严格 schema
  verify.py      完整验证流程与结构化报告
  runner.py      验证门禁：只有验证通过才 spawn entrypoint
  server.py      仅 loopback 的 HTTP 服务（标准库 http.server）
  cli.py         命令行（keygen/sign/verify/run/serve）
tests/           87 个自动化测试（unittest，无额外依赖）
examples/        请求样例、清单样例、一键本地演示脚本
```

---

## 3. 安装

无需安装即可使用（仓库根目录设 `PYTHONPATH=.`）。也可安装为命令：

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
# 或 pip install -e .
```

依赖只有 `cryptography`（实测 41.0.7，声明兼容 `>=41,<47`）。

---

## 4. 快速上手（CLI）

```bash
export PYTHONPATH=.

# 1) 本地生成测试密钥对（私钥 PEM + 公钥入信任库）
python3 -m sig_manifest keygen \
  --private-key ./work/testkey.pem \
  --trust-store ./work/trust.json \
  --no-password            # 测试用；交互运行不加此参数会提示输入口令（scrypt 加密 PEM）

# 2) 对制品目录签名（枚举所有普通文件，计算 SHA-256）
python3 -m sig_manifest sign \
  --artifact-root ./work/artifact \
  --name demo-app \
  --private-key ./work/testkey.pem --no-password \
  --entrypoint bin/hello.sh arg1 \
  --output ./work/manifest.json

# 3) 验证（摘要 + 签名 + 可信密钥 ID + 路径安全 + 无多余文件）
python3 -m sig_manifest verify \
  --artifact-root ./work/artifact \
  --manifest ./work/manifest.json \
  --trust-store ./work/trust.json
# 退出码 0 = 通过；非 0 = 失败（错误码稳定，见 errors.py）

# 4) 验证通过后才执行（失败绝不 spawn）
python3 -m sig_manifest run \
  --artifact-root ./work/artifact \
  --manifest ./work/manifest.json \
  --trust-store ./work/trust.json --no-password \
  extra-cli-arg
```

一键脚本演示完整流程与篡改场景：`bash examples/demo_local_flow.sh`。

---

## 5. 清单格式

信封（envelope）：

```json
{
  "signed": {
    "manifest_version": 1,
    "type": "artifact-manifest/v1",
    "artifact_name": "demo-app",
    "created_at": "2026-09-23T17:50:49Z",
    "nonce": "a5c8705a766bac16d596a038ad9a286c",
    "files": [
      {"path": "bin/hello.sh", "size": 53,
       "digest_algorithm": "sha256",
       "digest": "8ae7da2e...22735"}
    ],
    "entrypoint": ["bin/hello.sh", "arg1"]
  },
  "signatures": [
    {"key_id": "ed25519-sha256:542b22ca...d1abc",
     "algorithm": "ed25519",
     "signature": "<base64url 无填充的 64 字节 Ed25519 签名>"}
  ]
}
```

- **被签名的字节** = `signed` 对象经过规范化 JSON（§6）后的 UTF-8 字节。
  磁盘上的信封可以任意缩进、字段任意重排，验签前一律重新规范化。
- **密钥 ID** = `ed25519-sha256:` + 原始 32 字节公钥的 SHA-256 十六进制。
  信任库存公钥时会重算 ID 并与记录比对，防止"换公钥沿用旧 ID"。
- schema 严格：信封/签名/文件条目多一个或少一个字段都拒绝；
  `entrypoint` 可省略，但一旦给出，其首元素必须是清单内文件、且为安全相对路径。
- `files` 是**有序数组**：数组顺序是签名载荷的一部分（仅对象键做排序）。

信任库 `trust.json`：只保存可信公钥，`version/kind/keys[]`，加载时同样走严格
JSON（重复键拒绝），私钥文件权限强制 `0600`。

---

## 6. 规范化 JSON 的明确规则

实现见 `sig_manifest/canonjson.py`。规则刻意做窄：

1. 只允许对象、数组、字符串、整数、`true/false`、`null`；
   **禁止浮点数**（`1` 与 `1.0` 必须只有一种表示）、禁止 `NaN/Infinity`；
2. 顶层必须是对象；
3. **对象出现重复键即报错**——不接受多数解析器"后者覆盖前者"的默认行为；
4. 字符串与对象键统一 **NFC 归一化**；归一化后键冲突也报错；拒绝孤立代理项；
5. 对象键按 **Unicode 码点排序**；
6. 输出为 **UTF-8 紧凑**格式，无任何多余空白；
7. 非 ASCII 字符直接输出（不转 `\uXXXX`），仅转义引号、反斜杠、
   `\b \f \n \r \t` 与其余 C0/DEL 控制字符；
8. 拒绝 UTF-8 BOM、拒绝尾随内容。

与 RFC 8785 (JCS) 思路一致，差异是额外禁用浮点/指数以消除跨语言数字序列化分歧。

---

## 7. 路径安全（防逃逸）两道防线

实现见 `sig_manifest/paths.py`。清单中的路径必须是相对 POSIX 路径（用 `/`）。

1. **词法校验（不碰磁盘）**：拒绝绝对路径（`/...`、`//host/share`）、
   盘符（`C:`）、反斜杠、NUL/控制字符、空组件（`a//b`）、`.`、`..`、
   以 `/` 结尾。→ `../etc/passwd` 在访问文件系统之前就失败。
2. **realpath 包含校验**：把目标 realpath（解析全部符号链接）后确认仍位于
   制品根的 realpath 子树内。→ 词法合法但由符号链接引到根外的逃逸被拦截；
   悬空符号链接直接报错。

验证时额外要求：制品目录中**不得存在清单未记录的普通文件**
（可用 `allow_extra_files` 显式放宽；签名/摘要校验不受影响）。

---

## 8. 验证流程与失败安全

`verify_artifact()` 依次检查并收集所有问题（`VerificationReport`）：

1. 信封严格解析 + schema（重复键、字段集合、路径词法、摘要格式……）；
2. 对规范化后的 `signed` 用信任库公钥验签：
   - 没有任何签名来自可信密钥 → `unknown_key`（失败）；
   - 可信密钥的签名值错误 → `bad_signature`（失败）；
   - 有可信有效签名 + 额外未知密钥签名 → 未知的仅告警；
3. 每个文件：安全解析路径 → 必须存在且为普通文件 → 大小与 SHA-256 全匹配；
4. 无多余文件；`entrypoint` 目标存在（可执行位缺失仅告警）。

**执行门禁**（`runner.verify_then_run`）：先调 `verify_artifact_or_raise`，
只有完全通过才调用 `subprocess.run`（不经 shell，argv 直接取自签名内容，
cwd 锁定为制品根，默认精简环境变量）。因此任何验证错误都在 spawn 之前抛出。

稳定错误码（`errors.py`）：`canonical_json_error / unsafe_path / key_store_error /
manifest_schema_error / unknown_key / bad_signature / digest_mismatch /
file_error / runner_error`，CLI 对每类使用不同退出码。

---

## 9. HTTP 服务（仅本机）

```bash
python3 -m sig_manifest serve --host 127.0.0.1 --port 8080 \
  --trust-store ./work/trust.json
```

| 方法/路径 | 说明 |
|---|---|
| `GET /healthz` | 存活探针 |
| `POST /sign` | 入参 `artifact_root/artifact_name/private_key_pem[/password/entrypoint]`，返回信封 |
| `POST /verify` | 入参 `artifact_root/manifest[/allow_extra_files]`；通过 200，失败 **422** 并附全部错误 |
| `POST /run` | 同 verify，支持 `dry_run/extra_args/timeout/env`；验证失败返回错误且不执行 |

请求样例见 [`examples/`](examples/)。约束：只允许绑定 `127.0.0.1/::1/localhost`；
Host 头非 loopback 拒绝；请求体上限 16 MiB、超时 30 秒；私钥仅随请求提交、不落盘。
`manifest` 推荐传原始 JSON 文本（字符串），这样服务端才能检测重复键；
传 JSON 对象无法检测重复键（解析时已被合并）。

---

## 10. Python API 速览

```python
from sig_manifest.keys import (
    TrustStore, generate_private_key, save_private_key_pem,
    load_private_key_pem, public_from_private,
)
from sig_manifest.manifest import sign_artifact
from sig_manifest.verify import verify_artifact

priv = generate_private_key()                         # 本地 CSPRNG
envelope = sign_artifact("./artifact", "demo", priv,
                         entrypoint=["bin/hello.sh"])
store = TrustStore()
store.add(public_from_private(priv))
report = verify_artifact("./artifact", manifest_bytes, store)
assert report.ok                                        # 否则 report.errors
```

---

## 11. 测试

```bash
python3 -m unittest discover -s tests -v
```

87 个测试，覆盖（与验收点一一对应）：

- **字段重排**：对象键任意重排/美化排版后规范化字节一致、验证通过；
  并明确数组顺序有意义；
- **重复 JSON 键**：顶层、嵌套、信任库、清单中的重复键全部拒绝；
- **路径穿越**：`../`、绝对路径、反斜杠、盘符、`//`、`.`、控制字符、
  指向根外的文件/目录符号链接、悬空链接；
- **文件缺失 / 摘要篡改 / 截断 / 多余文件**；
- **未知密钥 / 损坏签名 / 篡改 signed 内容**；
- **执行门禁**：未知密钥、摘要不符、文件缺失、路径穿越、签名损坏时
  `run` 均在 spawn 前抛对应异常（另有真实子进程的 CLI 端到端测试）；
- HTTP 服务真实 socket 端到端（200/422/400）。

实际执行命令、输出与结果见 [`RUN_REPORT.md`](RUN_REPORT.md)。

---

## 12. 安全边界与非目标

- 信任根是本地 `trust.json`：初始公钥的带外分发不在本项目范围。
- 本项目不防"持合法密钥的签名者自己签了一个含 `../` 路径的清单"之外的逻辑；
  但这类清单在验证侧仍会被路径安全检查拒绝（有对应测试）。
- 不做网络分发、撤销（CRL/OCSP）、时间戳权威、制品隔离沙箱；
  `run` 只是门禁，不是沙箱（不限制已通过验证程序自身的行为）。
- 仅本地 loopback 服务，不提供 TLS/鉴权（依赖本机网络边界）。
- 不包含任何前端代码。
- **TOCTOU 说明**：`verify` 与随后的 `run` 是两步操作，理论上在两者之间
  替换文件可形成检查时/使用时竞争。本项目面向本地单用户离线场景；对更高
  安全要求，应在同一进程内紧接着调用 `runner.verify_then_run`（已把验证与
  spawn 放在同一函数中、间隔最小化），或以只读/不可变方式挂载制品目录。
  项目不做内核级文件冻结。
