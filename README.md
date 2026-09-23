# 合约编译产物溯源服务（Solidity Build Provenance）

一个**纯后端、完全离线**的服务：接收①源码压缩包、②编译配置、③已生成的编译器输出
（及可选的链上部署字节码），对三者做真实的密码学核验，并把部署字节码的
**元数据（CBOR auxdata）**与**库链接占位地址**两类可解释差异分离开，给出三级结论：

| 结论 | 含义 |
|------|------|
| `EXACT_MATCH` | 完全匹配：源码摘要、编译版本、配置、元数据哈希、库地址、代码体全部一致 |
| `RULE_MATCH`  | 规则内匹配：仅**元数据内容哈希**和/或**已链接库地址**不同，代码体逐 nibble 相同 |
| `MISMATCH`    | 无法匹配：任何其他差异（源码被改、优化配置/EVM 版本不同、代码体差异、地址校验失败、路径逃逸等） |

**绝不忽略任意差异**：除上述两类规则内差异外，一个 nibble 的不同都会被记录为
`BODY_DIFF`；路径规范化只作用于归档成员名，不可能掩盖源码语义变化。
**绝不执行上传包内脚本**：包只作为数据读取，脚本成员被显式登记为“未执行”。

---

## 1. 目录结构

```
provenance_service/
  crypto.py          # 真实密码学原语：Keccak-256、EIP-55、base58btc/CID、Ed25519、规范JSON、哈希链
  cbor.py            # 最小 CBOR 编解码 + solc auxdata 尾([cbor][2字节长度][0033])解析
  package_safety.py  # zip-slip/绝对路径/符号链接/设备节点/NUL/zip-bomb 防护（内存中解包）
  fixtures.py        # 离线构建“结构忠实”的 solc 产物（真实哈希依赖，不依赖 solc，不执行任何脚本）
  bytecode.py        # auxdata 尾分离、库占位槽定位/掩码、逐区域比较
  verifier.py        # 核心核验引擎（路径/摘要/版本/配置/元数据承诺/占位符/地址/代码体）
  models.py          # 结论级别与全部 finding code 定义
  storage.py         # SQLite(WAL) 只增证据库 + Ed25519 收据签名 + 跨作业哈希链
  app.py             # FastAPI（multipart 上传、查询、findings、收据验签）
scripts/
  generate_examples.py  # 生成 8 类示例输入到 examples/packages/
  accept.sh             # 一键验收（建环境→测试→起服务→8 样例→独立验签→脚本未执行检查）
tests/                 # 101 个自动化测试（公开密码向量 / CBOR / 路径攻击 / 全部结论 / HTTP）
examples/packages/     # 8 个可直接提交的示例（zip + config + compiler_output + runtime）
requirements.txt       # 直接依赖（固定版本）
requirements.lock      # pip freeze 完整锁定（29 个包）
requirements-dev.txt
```

## 2. 本地启动

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt        # 或 pip install -r requirements.lock 复现精确版本

python scripts/generate_examples.py    # 首次生成示例输入
uvicorn provenance_service.app:app --host 127.0.0.1 --port 8088
# 数据库 data/provenance.db（WAL）、Ed25519 收据私钥 data/receipt_key.pem 自动创建(0600)
```

可选环境变量：`PROVENANCE_DB`、`PROVENANCE_KEY`。

## 3. 验收命令

```bash
# 一键：建虚拟环境 + 跑测试 + 起服务 + 提交 8 个示例 + 独立验签 + 脚本未执行检查
./scripts/accept.sh

# 仅跑自动化测试（101 个）
source .venv/bin/activate
python -m pytest -q
```

手动 curl（服务已在 8088 启动）：

```bash
curl -s http://127.0.0.1:8088/health

curl -s -X POST http://127.0.0.1:8088/api/v1/verify \
  -F "package=@examples/packages/01_exact/sources.zip;type=application/zip" \
  -F "config=<examples/packages/01_exact/config.json" \
  -F "compiler_output=<examples/packages/01_exact/compiler_output.json" \
  -F "expected_runtime_code=<examples/packages/01_exact/expected_runtime_code.json"

JOB=<返回 JSON 中的 job_id>
curl -s http://127.0.0.1:8088/api/v1/jobs/$JID/receipt      # Ed25519 收据（含 signature_valid）
curl -s http://127.0.0.1:8088/api/v1/jobs/$JID/findings     # 全部可复核 finding
curl -s http://127.0.0.1:8088/api/v1/jobs                   # 作业列表（按链序倒序）
```

## 4. 协议（请求 / 响应）

`POST /api/v1/verify`，`multipart/form-data`：

| 字段 | 内容 |
|------|------|
| `package` | 声称被编译的源码 zip（**只读取，不执行其中任何文件**） |
| `config` | JSON：`compiler_version`、`optimizer{enabled,runs}`、`evm_version`、`libraries{fqn: EIP-55 地址}` |
| `compiler_output` | JSON，solc standard-json 形状（`contracts.<path>.<name>.metadata` 与 `evm.deployedBytecode`） |
| `expected_runtime_code` | 可选 JSON：`{"<path>:<Contract>": "0x<linked deployed runtime hex>"}`；省略则只做完整性核验 |

响应（200 正常；422 表示源码包被拒绝 REJECTED；400 表示请求 JSON 非法）节选：

```json
{
  "job_id": "…",
  "status": "OK",
  "verdict": "EXACT_MATCH | RULE_MATCH | MISMATCH",
  "package_digests": {"src/Token.sol": {"sha256": "…", "keccak256": "0x…"}},
  "contracts": [{
    "fqn": "src/Token.sol:Token",
    "verdict": "RULE_MATCH",
    "findings": [{"code": "BODY_OK", "severity": "FACT", "message": "…", "detail": {}}],
    "normalized_body_digest_expected": "0x…",
    "normalized_body_digest_observed": "0x…",
    "compared_library_slots": [{"start_byte": 113, "library_fqn": "…",
        "observed_address": "0x…", "declared_address": "0x…"}],
    "metadata_tail": {"length_field": 49, "fields": {"ipfs": "0x…", "solc": "0x000818"}}
  }],
  "warnings": [{"code": "SOURCE_EXTRA", "…": "scripts/build.sh 未被编译器输出引用"}],
  "package_findings": [{"code": "SCRIPT_NOT_EXECUTED", "…": "脚本成员仅作为数据"}],
  "evidence": {
    "payload_digest_keccak256": "…",
    "finding_chain": [{"fqn": "…", "chain": "…"}],
    "receipt_chain_head": "…"
  },
  "receipt": {"job_id": "…", "payload_digest": "…", "prev_chain": "…",
              "public_key": "<Ed25519 公钥 hex>", "signature": "<Ed25519 签名 hex>"}
}
```

## 5. 核验项与三级判定如何得出

按顺序真实执行（每一步都落 finding，无静默通过）：

1. **包安全**：成员名规范化（`\`→`/`、拒绝对路径/盘符/`..`/NUL）；拒绝符号链接、
   设备/FIFO/socket；重复规范化路径拒绝；压缩包大小、成员数、单文件与总解压大小、
   压缩比限制（防 zip-bomb）。任何违例 → 整个作业 `REJECTED / MISMATCH`。
2. **路径存在性**：编译器输出 `metadata.sources` 中的每个路径都必须能在包内解析到，
   否则 `SOURCE_MISSING`。
3. **源码摘要**：对包内实际字节重算 **Keccak-256**，与编译器输出/metadata 中记录的
   逐源摘要比对，不同 → `SOURCE_DIGEST_DIFF`。
4. **版本 / 配置**：提交的 `compiler_version`、`optimizer{enabled,runs}`、`evm_version`
   与 metadata `compiler.version` / `settings` 精确比对（`VERSION_DIFF` / `SETTING_DIFF`）。
5. **auxdata 元数据承诺**：真实解析尾部 CBOR；按 `ipfs`/`bzzr0`/`bzzr1`/`keccak256`
   **重算嵌入 metadata 的哈希**并比对（`METADATA_HASH_OK/DIFF`）；`solc` 版本字节再与
   metadata 交叉核对。尾损坏 → `METADATA_TAIL_MALFORMED`，绝不猜测。
6. **库占位符**：识别 solc 的 `__$<keccak16(fqn) 的 34 hex>$__` 34 字符占位符，校验其
   哈希确实提交到所引用库的全限定名（`PLACEHOLDER_BAD_SCHEME`），且与
   `linkReferences` 声明的偏移/长度一致（`LINK_REF_BAD`）。
7. **链接地址**：配置中的地址必须通过 **EIP-55** 校验；部署码中槽位地址必须有对应库
   映射声明（`ADDRESS_UNDECLARED`）；部署码仍含占位符 → `PLACEHOLDER_UNRESOLVED`。
8. **代码体三区域比较**（长度不同直接 `RUNTIME_LEN_DIFF`）：
   - auxdata 尾：逐字段比较，只有内容哈希键（`ipfs/bzzr*/keccak256`）允许不同
     （`METADATA_TAIL_DIFF_HASH`，规则内）；其余字段不同 → `METADATA_TAIL_DIFF_OTHER`；
   - 20 字节库槽：偏移必须严格对齐，内容（占位符/两个地址）允许不同
     （`LIBRARY_ADDRESS_DIFF`，规则内）；
   - **其余全部代码**：逐 nibble 比较，掩码后摘要化，任何不同 → `BODY_DIFF`。

存在任意 `ERROR` → `MISMATCH`；否则存在 `ALLOWED`（仅元数据哈希 / 库地址）→
`RULE_MATCH`；全为 `FACT` → `EXACT_MATCH`。

## 6. 证据链（可复核）

- 每个文件记录 `sha256` 与 `keccak256`；结果体用**规范 JSON**（键排序、无空白）摘要。
- 合约 findings 形成 Keccak 哈希链（`finding_chain`），相同输入确定性复现。
- 作业存入 SQLite（WAL、外键）：原始包、配置、编译器输出、结果、逐条 findings。
- 每个作业用服务 **Ed25519** 私钥对
  `{job_id,payload_digest,prev_chain,public_key}` 的规范 JSON 签名；`prev_chain`
  指向上一作业链头，形成只增链。`/receipt` 返回服务端验签结果，客户端可用
  `public_key` 独立验签（验收脚本即如此做）。篡改任一历史结果都会使链与签名失效。

## 7. “规范化不掩盖语义变化”的证明

- `07_path_relocation_rule`：相同源码内容、相同配置，仅编译路径不同 → 仅 auxdata
  内容哈希与库地址不同，代码体逐 nibble 相同 → `RULE_MATCH`。
- `06_semantic_change`：**相同路径**，只把一个常量从 `1_000_000` 改为 `2_000_000`
  （或重编译后比对）→ 同时触发 `SOURCE_DIGEST_DIFF` 与 `BODY_DIFF` → `MISMATCH`。
- 单 nibble 篡改代码区（测试 `test_single_nibble_tamper_in_code_is_caught`）必被捕获。
- fixture 的“代码生成”种子只依赖**源码内容摘要 + 编译设置**，不依赖路径（路径只进
  metadata），与真实 solc 语义一致；因此路径差异可被规则化，而任何源码/设置的语义
  变化必然落到代码体或摘要检查上。

## 8. 示例场景一览（`examples/packages/`）

| 目录 | 场景 | 期望 |
|------|------|------|
| `01_exact` | 完整一致构建并链接 | EXACT_MATCH |
| `02_library_address_rule` | 同一产物库被部署到第二个地址 | RULE_MATCH |
| `03_optimizer_mismatch` | 提交的优化配置与产物记录不符 | MISMATCH |
| `04_missing_source` | 编译器引用的 SafeMath 源码缺失 | MISMATCH |
| `05_malicious_path` | zip 内含 `../../etc/cron.d/evil` | REJECTED(422) |
| `06_semantic_change` | 改了常量却声称产物没变 | MISMATCH |
| `07_path_relocation_rule` | 等价构建路径搬迁 | RULE_MATCH |
| `08_no_runtime_code` | 不传部署码，仅完整性核验 | EXACT_MATCH |

测试还覆盖：优化开关重编译导致代码体差异、EVM 版本差异、编译器版本差异、坏 EIP-55、
未声明库、未解析占位符、运行码长度差异、auxdata 损坏、符号链接/设备/NUL 成员、
zip-bomb、收据签名与跨作业链等。

## 9. 关于离线与“真实执行”

- 不依赖、也不调用 `solc`：服务定位是**核验已生成产物**。示例产物由 `fixtures.py`
  离线合成，但其结构忠实于真实 solc 输出——auxdata CBOR 真实编码并以真实
  sha2/keccak 提交 metadata、占位符采用真实 `__$keccak16(fqn)$__` 方案、代码体真实
  依赖源码摘要与编译设置。因此所有核验都是**真实重算与比对**，无桩值、无短路。
- 已用公开向量交叉验证密码学原语：Keccak-256 空串/`abc`（且区别于 NIST SHA3）、
  ERC-55 全部官方地址、base58btc 与空块 CIDv0
  `QmdfTbBqBPQ7VNxZEYEj14VmRuZBkqFbiwReogJgS1zR1n`、Ed25519 真签真验。
- 包内 `scripts/build.sh` 带有可执行位，仅被读取为字节并登记 `SCRIPT_NOT_EXECUTED`；
  若它被执行会留下 `/tmp/provenance_script_should_never_run.marker`，测试与验收脚本
  均断言该标记不存在。
