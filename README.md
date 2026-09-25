# SAM — 签名制品清单（Signed Artifact Manifest）

纯后端、离线的本地安全数据处理服务：对一个**制品目录**生成带密码学签名的
清单（manifest），并在使用制品前用**本地信任库**验证可信密钥 ID、签名与
每个文件的内容摘要。**验证不通过，绝不执行制品。**

- 密码原语全部来自成熟库 [`cryptography`](https://cryptography.io/)（Ed25519 / SHA-256），
  **不自创加密算法**；
- 测试密钥本地即时生成，**不接入任何生产账户 / KMS / 网络服务**；
- 提供 CLI 与仅监听 `127.0.0.1` 的本地 HTTP 服务；**无前端**。

---

## 1. 它解决什么问题

分发一个目录（脚本 + 配置 + 数据）时，使用方需要确认：

1. 它确实由某个**受信密钥**签名（而不是任意陌生人）；
2. 签名之后内容**没有被改动**（逐文件 SHA-256）；
3. 清单里声明的文件**没有缺失**，目录里也**没有混入未签名文件**；
4. 清单元数据里的相对路径**无法逃逸**制品根目录（`../`、绝对路径、符号链接等）；
5. 待执行的入口程序（entrypoint）本身也在签名覆盖范围内。

## 2. 威胁模型与边界

**覆盖：** 意外损坏、本地/链路篡改、用不受信密钥签名、路径穿越、
符号链接逃逸、清单外文件植入、JSON 解析歧义（重复键、字段重排）、
签名后对元数据的任何修改。

**不覆盖（有意为之）：**

- 不做密钥分发 / 撤销：信任锚点是本地 PEM 文件目录，如何把公钥放进
  `trust/` 由运维流程负责；
- 不做时间戳/在线吊销：纯离线，`created_at` 仅为信息字段，不参与信任决策；
- 不沙箱化制品行为：`run` 只保证“跑的是验签通过的字节”，不限制制品运行后做什么；
- 不防本机已能读取你私钥文件的攻击者；私钥文件权限默认 `0600`。

## 3. 数据模型

封套（envelope）JSON：

```json
{
  "sam_version": 1,
  "algorithm": "ed25519",
  "manifest": {
    "sam_version": 1,
    "algorithm": "ed25519",
    "key_id": "<64 位小写十六进制>",
    "created_at": "2026-09-23T18:03:15Z",
    "entrypoint": "bin/app.sh",
    "files": [
      {"path": "bin/app.sh", "sha256": "<64 hex>", "size": 134}
    ]
  },
  "signature": "<Ed25519 签名的 base64>"
}
```

- **密钥 ID** = `SHA-256(DER 编码的 SubjectPublicKeyInfo)`，小写十六进制；
- **签名精确覆盖** `canonical(manifest)` 的 UTF-8 字节（见下节）；
- 封套自身的字段顺序、缩进、空白**不影响**验签——因此字段重排安全；
- `files` 是**数组（有序）**，条目顺序改变会改变规范字节并使签名失效；
- 封套文件必须放在制品根**之外**，否则严格模式会把它当成清单外污染文件。

## 4. 规范化 JSON（明确规则）

签名/摘要只针对规范化字节，规则在 `sam/canonical.py` 中显式实现并附测试：

1. 固定 UTF-8；输入带 BOM 直接拒绝（不静默剥离）；
2. 拒绝 NaN / Infinity / -Infinity；
3. **对象内重复键一律拒绝**（解析阶段，嵌套对象同样生效）；
4. 拒绝文档末尾多余字节；顶层必须是对象或数组；
5. 对象按键的 **Unicode 码位**排序（区别于 RFC 8785 的 UTF-16 码元序；
   本系统签名与验签用同一实现，互操作无歧义，此处有意选择更简单的规则）；
6. 无多余空白；非 ASCII 字符按 UTF-8 原样输出（不转义 `\uXXXX`）；
   仅转义 `"` `\` 和 U+0000–U+001F；孤立代理项拒绝；
7. 整数最短十进制（任意精度）；有限浮点用最短往返表示（Python `repr`）；
8. 拒绝 set/bytes/datetime 等非 JSON 类型；往返稳定
   `canonical(parse(x)) == canonical(parse(canonical(parse(x))))`。

## 5. 路径安全（双重防护）

1. **词法检查**（`validate_relative`，碰文件系统之前）：只接受 POSIX 风格
   相对路径；拒绝绝对路径、驱动器号、反斜杠（跨平台语义歧义）、NUL、
   空段、`.`、`..`、以空格或点结尾的路径段；
2. **解析后包含检查**（`resolve_within`）：`Path.resolve()` 展开全部
   符号链接后，结果必须仍在已解析的制品根内——阻止“词法合法、链接指向外部”；
3. 枚举制品时：目录软链接拒绝（防换根/递归环），FIFO/设备/套接字拒绝，
   文件软链接仅允许指向根内普通文件。

## 6. 验证链（顺序）

`sam verify` 只读、不执行；问题**尽量全部收集**（纵深防御，一次给出全部异常）：

1. 严格解析封套（重复键在这里即被拒）；封套/清单 schema 校验；
2. `manifest.key_id` 是否在本地信任库 → 未知则 `UNKNOWN_KEY`；
3. Ed25519 验签 `signature` 对 `canonical(manifest)` → `SIGNATURE_INVALID`；
4. 逐文件：路径安全 → 存在性（`FILE_MISSING`）→ 大小（`SIZE_MISMATCH`）
   → SHA-256（`DIGEST_MISMATCH`）；
5. 目录中存在清单外文件 → `EXTRA_FILE`（默认严格失败，`--allow-extra` 可放宽）；
6. `entrypoint` 路径合法且必须在 `files` 清单内。

`sam run` 走的是“失败即抛异常”的 `verify_or_raise`，**任何一条问题都不会执行**。

## 7. 安装

```bash
python3 -m venv .venv && . .venv/bin/activate    # 可选
pip install -r requirements.txt                  # cryptography>=41
```

## 8. CLI 用法

```bash
# 生成测试密钥（私钥 0600）
python3 -m sam keygen --private-key keys/key.pem --public-key trust/key.pem

# 对目录离线签名（封套输出到制品根之外）
python3 -m sam sign --artifact-root ./artifact \
  --private-key keys/key.pem \
  --output envelope.sam.json --entrypoint bin/app.sh

# 验证（人类可读；--json 输出机器可读报告）
python3 -m sam verify --artifact-root ./artifact \
  --envelope envelope.sam.json --trust-dir ./trust [--json] [--allow-extra]

# 先验证后执行（--python 用解释器跑脚本；否则要求 entrypoint 自带可执行位）
python3 -m sam run --artifact-root ./artifact \
  --envelope envelope.sam.json --trust-dir ./trust \
  --python -- 参数1 参数2
```

退出码：`0` 成功；`2` 验证失败（报告 ok=false）；`3` 用法/环境错误；
`run` 验证通过后透传制品自身的退出码。

完整可运行样例：`bash examples/cli-commands.sh`。

## 9. 本地 HTTP 服务（可选）

```bash
python3 -m sam.service --host 127.0.0.1 --port 18088 \
  --signing-key keys/key.pem      # 不传 --signing-key 则 /v1/sign 返回 403
```

| 方法 | 路径 | 说明 |
|---|---|---|
| GET  | `/healthz` | 健康检查、签名端点是否启用 |
| POST | `/v1/sign` | `{entrypoint?, files:{path:{text|b64}}}` → 封套 |
| POST | `/v1/verify` | `{envelope, trusted_public_keys:[pem-b64], files}` → 验证报告 |

- 仅绑定回环；绑定非回环地址会打印明确警告（服务本身无鉴权）；
- 请求体内联提供文件内容，服务在临时目录物化并立即清理；请求上限 32 MiB；
- `/v1/verify` 的信任公钥由**每次请求显式给出**，服务不内置隐式信任；
  业务上的验签失败仍返回 HTTP 200，结论看 `body.verify.ok`；
  请求格式错误返回 400，签名端点未启用返回 403。

curl 样例：`examples/http/curl.sh`；请求体样例：`examples/http/*.json`。

## 10. 代码结构

```
sam/
  canonical.py    规范化 JSON（严格解析 + 确定性编码）
  safepaths.py    路径词法校验、解析后包含检查、制品枚举
  keys.py         Ed25519 密钥生成/PEM/签名原语、信任库、key_id
  manifest.py     清单/封套数据模型、文件 SHA-256
  sign.py         离线签名编排、keygen 落盘
  verify.py       验证链 + VerifyReport/问题码（只读, 不执行）
  runner.py       验证通过后才执行 entrypoint
  cli.py          keygen/sign/verify/run
  service.py      回环 HTTP 服务
tests/            pytest 自动化测试（86 个用例）
examples/         CLI 与 HTTP 请求样例
RUNLOG.md         本机真实运行记录（命令与结果）
```

## 11. 运行测试

```bash
python3 -m pytest -q
```

覆盖（对应验收点）：

- 规范化 JSON：字段重排一致、重复键拒绝、BOM/NaN/尾随字节、非 ASCII、浮点/大整数；
- 路径安全：`../`、绝对路径、反斜杠、驱动器号、NUL、符号链接（文件/目录）
  逃逸、FIFO；
- 密钥：Ed25519 往返、篡改签名/消息失败、key_id 确定性、信任库重复/垃圾/空目录；
- 端到端：**字段重排验签通过、files 数组改序验签失败、重复 JSON 键拒绝、
  路径穿越、文件缺失、未知密钥、内容/大小篡改、签名位翻转、元数据事后篡改、
  算法篡改、清单外文件、entrypoint 逃逸、畸形 JSON**；
- CLI：完整流程、退出码、**篡改后 run 退出非零且制品无输出（未执行）**；
- HTTP：健康检查、签名/验证往返、篡改检测、穿越 400、空信任库 400、无签名密钥 403。

## 12. 已知限制

- 仅 Ed25519 一种算法（`algorithm` 字段为前向兼容保留，未知值直接失败）；
- 制品枚举按静态文件集；不感知挂载点/运行时生成文件；
- HTTP 服务无鉴权与 TLS（只监听本机回环，设计为被本机可信进程调用）。
