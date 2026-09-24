# 审计日志防篡改证明（Tamper-evident Audit Log）

纯后端服务（Python + FastAPI + `cryptography`）。审计记录只能追加，每条记录以
**规范化 JSON + SHA-256** 构成哈希链；服务端持有 Ed25519 签名私钥，定期对链头
签名形成**检查点（checkpoint）**；验证者独立持有公钥（信任锚），可对导出区间做
独立验证，发现删改、重排、截尾与伪造检查点，并区分：

* **TRUNCATED**——可由最新可信检查点（或验证者缓存的外部锚）证明的截断；
* **UNDETERMINED**——最新锚之后存在干净尾部：内容可校验，但缺少新锚时完整性
  不可判定（尾部被静默截掉与日志本来就短无法区分）。

无前端界面，仅 HTTP JSON 接口 + 一个独立命令行验证器。

---

## 1. 目录结构

```
app/
  canonical.py   规范化编码（sort_keys、紧凑分隔、UTF-8）与 SHA-256
  keys.py        Ed25519 密钥生成/加载/签名/验签
  chain.py       记录与检查点的构造、哈希、签名结构
  store.py       追加式 JSONL 存储（records.jsonl / checkpoints.jsonl，fsync）
  verifier.py    独立验证逻辑（四态判定，无状态、不依赖服务器）
  config.py      环境变量配置
  schemas.py     请求模型
  main.py        FastAPI 应用、定时检查点后台任务、HTTP 接口
tools/
  keygen.py          生成服务端签名密钥对（私钥留服务器，公钥即信任锚）
  verify_export.py   验证者 CLI（信任锚由验证者自己持有）
scripts/demo.sh      一键端到端演示（追加→签名→篡改矩阵→验证）
tests/               pytest 自动化测试（28 个用例）
requirements.txt     直接运行依赖（3 个，钉版本）
requirements-lock.txt  完整锁定依赖（含间接依赖与测试工具，钉死版本）
```

## 2. 依赖与安装

需要 Python ≥ 3.10（实测 Python 3.12.3 / Linux）。

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-lock.txt   # 或 requirements.txt
```

直接运行依赖只有三个：`fastapi`、`uvicorn[standard]`、`cryptography`
（Ed25519 与哈希均由 OpenSSL 支持，纯标准库之外无其他密码学代码）。
`pytest`、`httpx` 仅用于测试，已包含在 lock 文件中。

## 3. 启动

```bash
# 方式 A：自动生成密钥（演示用；公钥会写在私钥旁，仅供本地演示）
AUDIT_DATA_DIR=./data \
AUDIT_SIGNING_KEY=./keys/audit_signing_key.pem \
AUDIT_CHECKPOINT_INTERVAL=5 \
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8077

# 方式 B：运维先生成密钥，再把私钥放到服务器、公钥通过带外渠道交给验证者
.venv/bin/python -m tools.keygen --key-dir keys
AUDIT_SIGNING_KEY=./keys/audit_signing_key.pem \
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8077
```

环境变量：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `AUDIT_DATA_DIR` | `./data` | 两个 JSONL 文件目录 |
| `AUDIT_SIGNING_KEY` | `./keys/audit_signing_key.pem` | Ed25519 私钥 PEM（不存在则自动生成） |
| `AUDIT_CHECKPOINT_INTERVAL` | `5` | 后台自动签名间隔秒；链头未变化时自动跳过 |
| `AUDIT_CHECKPOINT_ON_APPEND` | `false` | 为 true 时每条记录后立即签名 |

启动时若日志为空会自动写一个 **seq=0 创世锚**（对全零哈希签名），
从而可以证明“日志确实为空”，而不是“被清空”。

## 4. HTTP 接口与请求样例

### 追加记录

```bash
curl -s -X POST http://127.0.0.1:8077/records \
  -H 'Content-Type: application/json' \
  -d '{"actor":"alice","action":"login","resource":"/session/1",
       "payload":{"ip":"10.0.0.1"}}'
```

记录字段：`seq`（从 1 递增）、`ts`（UTC ISO-8601）、`actor`、`action`、
`resource`、`payload`（任意 JSON）、`prev_hash`、`hash`。

### 查询记录（闭区间，1 起）

```bash
curl -s 'http://127.0.0.1:8077/records?start=2&end=10'
```

### 立即签名检查点 / 列出检查点

```bash
curl -s -X POST http://127.0.0.1:8077/checkpoints
curl -s http://127.0.0.1:8077/checkpoints
```

### 导出（全量或区间）

```bash
curl -s http://127.0.0.1:8077/export -o export.json
curl -s 'http://127.0.0.1:8077/export?start=4&end=5' -o slice.json
```

导出包含：记录切片、全部检查点、`start/end`、`total_records`、`includes_tail`
等元数据。**这些元数据只作参考，验证者一律重新计算，不信任服务器自报值。**

### 无状态参考验证接口（POST /verify）

调用方自带信任锚与导出包，服务器不保存也无法知道验证者信任谁：

```bash
ANCHOR=$(curl -s http://127.0.0.1:8077/anchor/public-key | python3 -c 'import json,sys;print(json.load(sys.stdin)["public_key_hex"])')
curl -s -X POST http://127.0.0.1:8077/verify \
  -H 'Content-Type: application/json' \
  -d "{\"trust_anchor\":\"$ANCHOR\",\"bundle\":$(cat export.json)}"
```

> `GET /anchor/public-key` 仅为本地演示便利。**生产环境绝不能从被审计的
> 服务器取公钥**——应通过运维渠道分发 `audit_signing_key.public.pem`。

## 5. 独立验证（信任锚由验证者持有）

```bash
# 全量导出
.venv/bin/python -m tools.verify_export \
  --anchor keys/audit_signing_key.public.pem --bundle export.json

# 有界区间（检查点超出区间末端不算截断）
.venv/bin/python -m tools.verify_export --anchor key.public.pem \
  --bundle slice.json --bounded-range

# 验证者自带“最新锚缓存”：上次审计记住 seq 最大的检查点，
# 以后即使服务器重写了自己导出的检查点列表，回滚/截断仍会暴露
.venv/bin/python -m tools.verify_export --anchor key.public.pem \
  --bundle export.json --remember anchor-cache.json
```

退出码：`0 VALID` / `3 UNDETERMINED` / `4 TRUNCATED` / `5 TAMPERED`。

一键演示（先按第 3 节启动服务器）：`scripts/demo.sh`

## 6. 数据结构与密码学

* **规范化**：`json.dumps(sort_keys=True, separators=(",",":"), ensure_ascii=False)`
  的 UTF-8 字节；哈希为 SHA-256 hex。生产端与验证端共用同一函数。
* **记录哈希链**：

  ```
  body = {seq, ts, actor, action, resource, payload, prev_hash}
  hash = SHA256(canonical(body))
  record 1 的 prev_hash = 64 个 0（创世哈希）
  ```

* **检查点**：

  ```
  cp_body = {seq, record_hash, ts, prev_checkpoint_hash}
  signature = Ed25519_sign(canonical(cp_body))
  检查点之间也用 SHA256(canonical(cp_body)) 串链，删除/重排检查点可发现
  ```

* **信任锚**：Ed25519 公钥（raw 64 字节，hex 或 PEM）。验证者带外持有。

## 7. 判定语义（验收核心）

| 结论 | 触发条件 |
|---|---|
| `VALID` | 记录哈希全部重算一致、链连续，且有签名有效的检查点覆盖链头；空日志有 seq=0 锚 |
| `TAMPERED` | 记录自哈希不符；`prev_hash` 断裂、缺号（删除）、乱序；检查点签名验不过（**伪造检查点**，含他人密钥所签）；签名检查点与其覆盖记录不一致；检查点链断裂 |
| `TRUNCATED` | 可信检查点覆盖到 seq=N，但导出的已验证前缀在 N 之前结束（包括验证者缓存的外部锚） |
| `UNDETERMINED` | 硬性检查全部通过，但最新可信锚之后还有干净尾部；尾部**改动**仍判 TAMPERED，尾部**被截短**在缺少新锚时无法判定 |

攻击者即使重算了后续记录的自哈希让链自洽，也无法伪造检查点签名：
锚覆盖范围内的篡改一律 `TAMPERED`；删掉锚覆盖的记录则 `TRUNCATED`。
唯一不可证明的是“锚之后、尚未签名的尾部是否被截短”，这是哈希链+周期签名
方案的固有边界，系统如实返回 `UNDETERMINED` 而不是误报。

## 8. 自动化测试

```bash
.venv/bin/python -m pytest -q
```

覆盖：正常追加/重启持久化、创世锚、改字段（含重算自哈希后仍被链/锚发现）、
删除（缺号）、重排、删/重排检查点、伪造签名、他人密钥签名、检查点与记录
不一致、有锚截断、外部锚捕获回滚、干净尾部 UNDETERMINED、尾部改动仍被发现、
新锚落地后转 VALID、无锚空导出 UNDETERMINED、有界区间（有/无边界锚）等。

## 9. 威胁模型边界（未覆盖项）

* 签名私钥泄露后，持钥者可生成任意“合法”历史——信任边界止于私钥保密；
  建议配合 HSM/KMS、短轮换与多签扩展。
* 仅证明追加完整性与顺序，不提供保密性（记录明文存储）。
* 单进程存储锁；多副本/高可用部署需共享存储或额外复制协议（未实现）。
* `GET /anchor/public-key` 不可用于生产信任分发（接口响应里有显式警告）。
