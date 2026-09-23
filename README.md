# Solidity 编译产物离线溯源服务

纯后端服务：给定**源码包 + 编译配置 + 已生成的编译器输出（solc standard-json 产物）
+ 链上部署字节码**，在**完全离线、不调用编译器、不执行上传包内任何脚本**的前提下，
用真实密码学计算回答一个问题——

> 这份链上字节码，是否确实由这些源码、按这套配置、用这个编译器版本产生？

并给出三档可解释裁决与一条可离线复验的哈希链证据。

---

## 1. 裁决语义（任何差异都必须显式归类，禁止静默忽略）

| 裁决 | 含义 |
| --- | --- |
| `EXACT` | deployed 字节码与期望产物**逐 nibble（半字节）完全一致**，且源码摘要、编译器版本、优化配置等全部核验通过 |
| `RULE_MATCH` | 存在差异，但差异**只**出现在预先声明、语义可解释的规则区域；其余逐 nibble 一致 |
| `MISMATCH` | 存在任何无法被规则解释的差异，或任意密码学/元数据/源码核验失败 |

仅有的两条“规则区域”，以及它们允许掩盖的差异范围：

1. **`LINKED_LIBRARY_ADDRESSES`（链接库地址）**——solc 对外部库只产出 40 nibble
   占位（`__$keccak256(完全限定名)[:34]$__`），地址在部署时填入。掩码严格限定在
   `linkReferences` 声明的区间，且：
   - 占位原文必须与库 FQN 的 `keccak256` 真实绑定（防张冠李戴）；
   - 库地址必须 20 字节且通过 **EIP-55 校验和**；
   - 目标字节码对应位置不得残留占位；
   - 长度变化、占位区间外的任何差异一律 `MISMATCH`。
2. **`CBOR_METADATA_TAIL`（CBOR 元数据尾）**——solc 追加在字节码末尾的元数据段。
   掩码只覆盖 CBOR 段 + 2 字节长度，且要求两侧尾起点相同；期望侧内嵌的
   `ipfs`/`bzzr0`/`bzzr1` 哈希必须由服务端**重新计算并比对一致**（不是“有哈希就算过”）。

> 规范化绝不掩盖语义变化：优化开关/runs、viaIR、evmVersion、编译器版本三段、
> 字节码长度、代码主体——任何不一致都直接 `MISMATCH`。

---

## 2. 服务真实执行了哪些计算

- **源码摘要**：对元数据 `sources[*].keccak256` 逐一真实重算 Keccak-256
  （pycryptodome，以太坊填充，非 NIST SHA3），缺失/不符即失败。
- **编译器版本**：比对 `config.compilerVersion`、`metadata.compiler.version`、
  字节码 CBOR 尾中的 `solc` 三段版本（major/minor/patch），三者必须一致。
- **元数据哈希**：重算 `SHA-256(metadata JSON)`（ipfs，可还原 CIDv0）与
  Swarm BMT 哈希（bzzr0/bzzr1，单分块 BMT 真实实现；≥4096 字节的元数据超出本
  离线实现能力时**显式报告不可判定**，绝不放行）。
- **库占位**：真实计算 `keccak256(FQN)` 前 34 hex 校验占位绑定；EIP-55 用
  Keccak-256 实现，覆盖全部 8 个官方校验向量。
- **字节码比对**：nibble 级掩码比较，报告每段未解释差异的位置、期望值、实际值。
- **哈希链证据**：每条检查 `self_hash = SHA256(prev_hash ‖ 规范化check JSON)`，
  创世 `prev` 为 64 个 0；报告、原始输入全部落盘，事后任何篡改都能被
  `GET .../evidence` 重放检测到。

---

## 3. 目录结构

```
app/
  api.py             FastAPI 路由（multipart 上传、错误码、查询接口）
  service.py         核验主流水线（裁决逻辑、证据收集）
  compiler_output.py config 与 solc 产物解析（零容忍结构校验）
  metadata.py        CBOR 元数据尾（自实现确定性解码器）+ 元数据 JSON
  bytecode.py        hex 规范化、占位识别、nibble 掩码与差异分析
  libraries.py       库地址/EIP-55、占位哈希绑定、链接
  archive.py         zip/tar/tar.gz 安全解包（防穿越/符号链接/压缩炸弹）
  paths.py           严格路径规范（歧义路径一律拒绝）
  crypto.py          SHA-256 / Keccak-256 / BMT / EIP-55 / CIDv0 / Base58
  storage.py         SQLite 持久化 + 哈希链
  samples/generate.py 确定性示例生成器（真实哈希、真实 CBOR、真实占位）
tests/               88 个 pytest 用例
examples/            由生成器产出的 8 个场景 + 7 个恶意归档
acceptance.sh        本地一键验收
requirements.lock    pip freeze 全量锁定版本
```

---

## 4. 本地启动

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.lock

# 生成示例输入（也可直接使用仓库内已生成的 examples/）
python -m app.samples.generate --out examples

# 启动（默认 127.0.0.1:8000，数据目录可用 PROVENANCE_DATA_DIR 覆盖）
python -m app
# 或：uvicorn app.api:app --host 127.0.0.1 --port 8000
```

健康检查：

```bash
curl -s http://127.0.0.1:8000/health
```

---

## 5. API

### `POST /api/v1/verify` — `multipart/form-data`

| 字段 | 形式 | 必填 | 说明 |
| --- | --- | --- | --- |
| `config` | 文件或表单字符串 | 是 | 编译配置 JSON |
| `compilerOutput` | 文件或表单字符串 | 是 | solc 标准 JSON 输出 |
| `targetDeployed` | 文件或表单字符串 | 是 | 链上 deployed/runtime 字节码 hex |
| `package` | 归档文件 | 二选一 | `.zip` / `.tar` / `.tar.gz` |
| `sources` | 表单字符串(JSON) | 二选一 | `{"路径": "UTF-8 源码文本"}` |

`config` 示例：

```json
{
  "compilerVersion": "0.8.24+commit.e11b9ed9",
  "source": "src/Vault.sol",
  "name": "Vault",
  "optimizer": {"enabled": true, "runs": 200},
  "viaIR": false,
  "evmVersion": "cancun",
  "libraries": {"src/SafeMath.sol": {"SafeMath": "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed"}}
}
```

curl 示例：

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/verify \
  -F "config=@examples/01-exact/config.json;type=application/json" \
  -F "compilerOutput=@examples/01-exact/compilerOutput.json;type=application/json" \
  -F "targetDeployed=<examples/01-exact/target_deployed.hex" \
  -F "package=@examples/01-exact/sources.zip;type=application/zip"
```

请求不可受理（路径违规、非法 JSON、非法地址等）返回 `400` 与错误码：
`BAD_PACKAGE / BAD_CONFIG / BAD_COMPILER_OUTPUT / BAD_LIBRARY_ADDRESS /
BAD_TARGET_BYTECODE / BAD_JSON / BAD_SOURCES / SOURCE_CONFLICT /
MISSING_FIELD / MISSING_SOURCES / PAYLOAD_TOO_LARGE`。
注意：**结构合法但溯源失败是 `200 + verdict=MISMATCH`**，不是 4xx。

### 查询接口

| 路由 | 说明 |
| --- | --- |
| `GET /api/v1/jobs` | 任务列表 |
| `GET /api/v1/jobs/{id}` | 任务元数据（输入/报告摘要、链头） |
| `GET /api/v1/jobs/{id}/report` | 完整裁决报告（逐条检查） |
| `GET /api/v1/jobs/{id}/evidence` | 哈希链 + 链完整性重放结果 |

原始输入落盘于 `$PROVENANCE_DATA_DIR/jobs/{job_id}/`（config、compilerOutput、
target hex、逐源文件、manifest 摘要）。

---

## 6. 示例场景（`examples/`）

| 目录 | 预期裁决 | 场景 |
| --- | --- | --- |
| `01-exact` | `EXACT` | 无库，完全匹配 |
| `02-rule-libaddr` | `RULE_MATCH` | 仅链接库地址不同（其余一致） |
| `03-rule-metadata` | `RULE_MATCH` | 仅 CBOR 元数据尾不同（代码语义一致） |
| `04-mismatch-optimizer` | `MISMATCH` | 配置声称 runs=200，产物实为 runs=999999 |
| `05-missing-source` | `MISMATCH` | 源码包缺失被编译文件 |
| `06-mismatch-semantic` | `MISMATCH` | 代码主体被改动（掩码外差异） |
| `07-mismatch-version` | `MISMATCH` | config(0.8.24) 与产物记录(0.8.20) 不一致 |
| `08-bad-library-address` | `HTTP 400` | 库地址 EIP-55 校验失败 |
| `_malicious/` | 全部 `400` | zip-slip、绝对路径、反斜杠、NUL、重复条目、tar 符号链接/硬链接 |

> 示例是**确定性合成产物**（无 solc 依赖），但其哈希结构完全真实：源摘要是真实
> Keccak-256，CBOR 尾是真实 CBOR 编码且内嵌真实 `SHA-256(metadata)`，库占位是
> 真实 FQN 哈希，地址是 EIP-55 官方向量。因此服务端的每一步核验都在做真计算。

---

## 7. 验收命令

一键验收（建虚拟环境 → 装锁定依赖 → 生成样例 → 88 项测试 → 起服务 → 真实 HTTP
核对全部样例与恶意归档）：

```bash
./acceptance.sh
```

仅跑自动化测试：

```bash
source .venv/bin/activate
python -m pytest -v
```

人工快速核对：

```bash
source .venv/bin/activate
python -m app &              # 起服务
for d in examples/0*; do
  curl -s -X POST http://127.0.0.1:8000/api/v1/verify \
    -F "config=@$d/config.json;type=application/json" \
    -F "compilerOutput=@$d/compilerOutput.json;type=application/json" \
    -F "targetDeployed=<$d/target_deployed.hex" \
    -F "package=@$d/sources.zip;type=application/zip" \
    | python3 -c 'import sys,json;r=json.load(sys.stdin);print(r.get("report",{}).get("verdict", r))'
done
```

篡改检测演示：直接改 SQLite 中任一证据行，再查 `.../evidence`，
`chain_verification.valid` 会变为 `false` 并指出被篡改的 `seq`。

---

## 8. 安全边界（设计上的“不做什么”）

- **不编译**：只核验别人已产出的 compiler output，离线运行，无网络访问。
- **不执行**：源码包只做字节提取，绝不 spawn/exec/eval 包内脚本或构造代码对象。
- **不放行未知项**：CBOR 未知键保留进证据；超大 bzzr 元数据显式“不可判定”；
  目标残留占位、长度不一致、破损占位全部 `MISMATCH`/400。
- **解包防护**：路径穿越、绝对路径、盘符、反斜杠、NUL/控制字符（直扫 ZIP 中央
  目录原始字节）、符号链接/硬链接/设备节点、重复条目、条目数（≤2048）与解压
  总量（≤5 MiB）限制。
- **库地址**：要求显式 EIP-55 校验和形式，杜绝“同字节不同写法”的歧义。

## 9. 已知范围限制（如实报告）

- Swarm `bzzr0/bzzr1` 实现覆盖 solc 元数据实际所处的单分块域（<4096 字节）；
  更大输入返回显式不可判定，不冒充通过。
- 产物结构严格按 solc standard-json 校验；非标准字段结构按 400 拒绝而非猜测。
- 服务为单实例 SQLite（WAL）；高并发部署可替换 `storage.Storage` 实现。
