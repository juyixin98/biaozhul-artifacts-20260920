# 分层区组随机分配服务（合成实验）

一套从零实现的 **分层、区组（permuted-block）随机分配** 后端，面向可审计、
可复现的实验场景。所有数据与参与者均为**本地合成夹具**，不依赖任何生产
账号或真实业务数据。

技术栈：Python 3.12 · FastAPI · NumPy · SciPy · SQLite（WAL）。
随机机制本身**只用标准库 `hmac/hashlib`**，NumPy/SciPy 仅用于证据侧的
分布检验与无偏整数采样工具。

---

## 1. 它保证什么

| 要求 | 机制 |
|---|---|
| 分层身份与随机种子冻结 | 创建研究必须显式提交种子；契约内容指纹 `ctr_…`，种子指纹 `seed_…`，二者绑定 |
| 区组内比例明确 | 区组长 = `比例和 × block_multiple`；槽位铺设确定性保证整组各臂计数严格等于比例 |
| 不完整尾组处理明确 | `keep_open`（保留续填，默认）与 `seal_early`（封尾后停止，需管理员显式开新组） |
| 同对象重复请求回原分配 | 以 `subject_id` 为锚，重复请求 `outcome=replayed`，**不消耗任何随机数** |
| 特征改变不能偷偷重随 | 特征在首次分配时哈希固化；之后变更 → `409 FEATURES_CHANGED_AFTER_ALLOCATION`，原臂保留 |
| 分配隐藏 | 开放区组的完整置换**不落库**，持库者无法预知未来次序；种子明文只在本地保险库（0600） |
| 审计权限隔离 | investigator 登记/揭盲；auditor 只读证据且不能登记；admin 开组/封尾/封研究 |
| 不把"效果显著"当正确证明 | 证据只核机制（比例枚举、逐数复算）；统计检验结论一律表述为"未检出偏离" |
| 可恢复、可复现 | 随机数是 `(种子,研究,层,区组,抽取序号)` 的纯函数；重启/双跑逐对象一致（固定入组顺序） |

---

## 2. 目录结构（四类后端职责分离）

```
app/
  contracts.py            统计契约：臂/比例/分层因子/区组乘子/尾组策略/指纹
  core/                   估计内核（无 I/O）
    seed.py               种子持有、冻结、PBKDF2 持有证明、脱敏
    stream.py             HMAC 计数器模式随机流、无偏 Fisher–Yates、定位坐标
    allocator.py          槽位铺设 + 纯函数 assign_at_position
    identity.py           分层身份与特征冻结
    vault.py              本地种子保险库（0600 文件）
  storage/                SQLite：schema、连接（WAL/串行写事务）、仓储
  service.py              分配编排：幂等、尾组、事务边界、审计落库
  api/                    FastAPI：鉴权、角色断言、可解释 trace、错误信封
  evidence/               证据与诊断（只读）
    balance.py            枚举每区组比例；尾组不确定单列
    distribution.py       多固定种子扫描 + 卡方/游程检验（非证明）
    stream_verify.py      用种子逐数重算，与落盘记录比对
    trace.py / audit_log.py  可解释追踪与 JSONL 审计日志
  reproducibility/        复现实验：夹具、双跑/并发/恢复、JSON 报告、CLI
tests/
  reference.py            独立参考实现（仅 hashlib，不 import app）
  test_*.py               断言具体结果与失败类别
config/local.example.json 本地合成配置样例
examples/requests.sh      端到端请求样例
```

---

## 3. 安装与配置

### 3.1 依赖版本（验收环境实测）

```
Python 3.12.3
fastapi==0.141.1   uvicorn==0.54.0   pydantic==2.13.5
numpy==2.4.6       scipy==1.15.3
httpx==0.28.1      pytest==9.1.1     pytest-cov==7.1.0
```

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pip install pytest-cov   # 覆盖率（测试用）
```

### 3.2 配置

复制 `config/local.example.json` 并通过环境变量指定：

```bash
export RCT_CONFIG="$PWD/config/local.example.json"
```

| 变量 | 说明 | 默认 |
|---|---|---|
| `RCT_CONFIG` | 配置文件路径 | 无（用内置合成默认） |
| `RCT_DB` | SQLite 文件 | `./data/rct.db` |
| `RCT_SEEDS` | 种子保险库文件（0600） | `./data/seeds.json` |
| `RCT_LOG` | JSONL 审计日志 | `./data/audit.log` |
| `RCT_ENV` | 环境标签 | `local-synthetic` |

内置的三个**合成**令牌仅用于本机：
`synt-investigator-token` / `synt-auditor-token` / `synt-admin-token`。
生产部署必须在配置或 `RCT_TOKENS_JSON` 中覆盖。

生成新种子（需要时）：

```bash
python3 -c "import base64,secrets;print(base64.b64encode(secrets.token_bytes(32)).decode())"
```

> 注意：创建研究时**必须显式提交种子**，服务永不隐式生成。种子决定全部
> 随机流，务必在实验启动前冻结并离线留存。

---

## 4. 启动与请求样例

```bash
uvicorn app.asgi:app --host 127.0.0.1 --port 8000
# 另开终端：
bash examples/requests.sh
```

创建研究（2 臂 1:1，两个分层因子，区组长 4，尾组保留）：

```bash
SEED=$(python3 -c "import base64;print(base64.b64encode(b'rct-fixed-seed-v1-A'.ljust(32,b'0')).decode())")
curl -s -X POST http://127.0.0.1:8000/v1/studies \
  -H 'Authorization: Bearer synt-investigator-token' \
  -H 'Content-Type: application/json' -d "{
    \"study_id\":\"DEMO-1\",
    \"arms\":[{\"arm_id\":\"control\",\"ratio\":1},{\"arm_id\":\"treatment\",\"ratio\":1}],
    \"factors\":[{\"name\":\"center\",\"levels\":[\"C1\",\"C2\",\"C3\"]},
                 {\"name\":\"stage\",\"levels\":[\"early\",\"late\"]}],
    \"block_multiple\":2,
    \"tail_policy\":\"keep_open\",
    \"seed_base64\":\"$SEED\"}"
```

登记（建议携带 `Idempotency-Key` 与 `X-Request-ID`，网络重试安全）：

```bash
curl -s -X POST http://127.0.0.1:8000/v1/studies/DEMO-1/allocations \
  -H 'Authorization: Bearer synt-investigator-token' \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: demo-key-0' -H 'X-Request-ID: demo-req-0' \
  -d '{"subject_id":"P0","features":{"center":"C1","stage":"early"}}'
```

每个响应是 `{"data": ..., "trace": ...}`：`data` 给分配结果，`trace` 给
请求身份、版本、关键步骤、随机流来源；失败在 `error.category`，不确定
结论在 `trace.uncertainties`，二者与成功信息严格分开。

### 角色与端点

| 方法/路径 | investigator | auditor | admin |
|---|:-:|:-:|:-:|
| `POST /v1/studies` | ✓ | – | ✓ |
| `POST /v1/studies/{s}/allocations` | ✓ | – | – |
| `GET  /v1/studies/{s}/assignments/{id}` | ✓ | – | ✓ |
| `POST /v1/studies/{s}/blocks`（seal_early 开组） | – | – | ✓ |
| `POST /v1/studies/{s}/actions/seal-tails` | – | – | ✓ |
| `POST /v1/studies/{s}/actions/seal` | – | – | ✓ |
| `GET  /v1/studies/{s}/evidence/balance` | ✓ | ✓ | ✓ |
| `POST /v1/studies/{s}/evidence/verify` | – | ✓ | ✓ |
| `GET  /v1/studies/{s}/audit` | – | ✓ | ✓ |
| `POST /v1/diagnostics/distribution` | ✓ | ✓ | ✓ |

---

## 5. 测试与证据

### 5.1 单元/集成测试

```bash
python3 -m pytest -q
# 带覆盖率：
python3 -m pytest -q --cov=app --cov-report=term-missing
```

测试不是"接口能调通"，而是断言**具体结果与失败类别**，例如：

- `tests/test_stream_reference.py`：把独立参考实现预算的**冻结向量**
  （具体置换与逐位置臂别）与内核、端到端登记逐一比对；
- 重复请求断言 `outcome=replayed` 且臂/区组/位置/时间戳全相同；
- 特征篡改断言具体类别 `FEATURES_CHANGED_AFTER_ALLOCATION` 且原臂保留；
- 并发 24 对象断言 24 个唯一槽位、无丢失、整组 12:12；
- 尾组封闭后登记断言 `STRATUM_SEALED`；越权断言 `FORBIDDEN_ROLE`；
- 人为篡改库中臂别，均衡与流复核都必须 `FAIL`（而不是放过）。

**参考答案独立性**：`tests/reference.py` 只 import `hashlib/hmac/struct`，
以另一份独立写法重新实现同一规格；期望值由它离线预算后冻结为常量，
测试运行时不调用被测代码生成答案。

### 5.2 复现实验（端到端证据）

```bash
python3 -m app.reproducibility.cli --workdir ./data/repro --out ./data/repro/report.json
```

它对三套夹具（2 臂 1:1 keep_open、2 臂 1:1 seal_early、不等比 1:2）执行：

1. **串行固定顺序双跑**：全新两个文件库，逐对象臂/层/区组/位置必须一致；
2. **并发批次**：多线程同时登记，校验槽位无冲突、无对象丢失、整组比例；
3. **恢复**：复用同一 DB 与种子文件"重启"，旧对象回放、新对象正确接续；
4. **特征篡改 / 幂等回放 / 尾组封闭**；
5. 每个研究做均衡核算与随机流逐数复核；
6. 报告记录种子指纹、PRF、域标签、流定位坐标与数据文件位置。

退出码 `0` 当且仅当 `summary.overall == "PASS"`。

### 5.3 随机分布核验

`POST /v1/diagnostics/distribution`（或 `app.evidence.distribution`）在
**6 颗显式列出的固定种子**上扫描：小区组（K≤5）枚举全部 K! 个置换做卡方
拟合优度；逐入组位置做臂频率卡方；跨区组首槽做游程检验。

> 这些 p 值**不证明**随机性或正确性。固定种子是确定性算法；检验只回答
> "这批样本上是否能*检出*偏离均匀/独立"。正确性的硬证据是 §5.1 的冻结
> 向量与逐数复算、以及 §5.2 的整组比例枚举。报告与 trace 都显式声明
> 这一点（`STATISTICAL_NON_PROOF`）。

---

## 6. 随机机制规格（可逐数复核）

- **区组密钥**（HKDF 风格两段 HMAC）：
  `extract = HMAC_SHA256("rct/v1/derive/study-stratum-block", master_seed)`
  `key = HMAC_SHA256(extract, study_id | contract_fp | stratum_key | block_index_u64be)`
- **区组流**：计数器模式
  `HMAC_SHA256(key, "rct/v1/block-permutation" || counter_u64be)`，
  每个 SHA256 输出按 4 字节大端提供 8 个 uint32。
- **无偏整数**：`randbelow(n)` 用 `limit=2^32-(2^32 mod n)` 的拒绝采样。
- **置换**：无偏 Fisher–Yates（K-1 次抽取，在全排列上均匀）。
- **铺槽**：按臂声明顺序重复 `ratio × block_multiple`；
  位置 `p` 的臂 = `slots[permutation[p]]`。
- **定位坐标**：`(study_id, contract_fingerprint, stratum_key, block_index,
  purpose, draw_index)`。审计可随机抽查任意坐标，无需顺序重放整条流。

域分离：`block-permutation` / `tail-completion` / `roll` 使用不同标签。

---

## 7. 失败类别（稳定字符串）

`MISSING_CREDENTIALS`、`INVALID_TOKEN`、`FORBIDDEN_ROLE`、
`INVALID_CONTRACT`、`SEED_REQUIRED`、`INVALID_SEED`、
`STUDY_ALREADY_EXISTS`、`UNKNOWN_STUDY`、`STUDY_FROZEN`、
`SUBJECT_NOT_FOUND`、`MISSING_STRATUM_FACTOR`、`UNEXPECTED_STRATUM_FACTOR`、
`NON_DISCRETE_STRATUM_VALUE`、`EMPTY_STRATUM_VALUE`、
`FEATURES_CHANGED_AFTER_ALLOCATION`、`IDEMPOTENCY_KEY_REUSE_CONFLICT`、
`IDEMPOTENCY_KEY_MISMATCH`、`ALLOCATION_RACE`、`STRATUM_SEALED`、
`OPEN_TAIL_BLOCK`、`ENROLLMENT_CLOSED`、`RECOVERY_SEED_MISSING`、
`REPLAY_MISMATCH`、`VERIFICATION_FAILED`、`STATISTICAL_INCONCLUSIVE`。

不确定（非失败）：`INCOMPLETE_TAIL_BLOCK`、`TAIL_SEALED_INCOMPLETE`、
`IDEMPOTENT_REPLAY`、`STATISTICAL_NON_PROOF`、`ENUMERATION_SKIPPED`。

---

## 8. 干净目录复现（验收清单）

```bash
# 1) 安装
python3 --version            # 期望 3.12.x
pip install -r requirements.txt && pip install pytest-cov

# 2) 测试 + 覆盖率（期望全绿，覆盖率 ≥ 80%）
python3 -m pytest -q --cov=app --cov-report=term-missing

# 3) 复现实验（期望退出码 0，overall=PASS）
python3 -m app.reproducibility.cli --workdir ./data/repro --out ./data/repro/report.json

# 4) HTTP 端到端
uvicorn app.asgi:app --host 127.0.0.1 --port 8000 &
bash examples/requests.sh
```

每次运行的实际结果记录于 §9。

---

## 9. 本次验收实际执行结果

> 以下为在本环境（Python 3.12.3，Linux）实际运行的如实记录。

### 9.1 自动化测试
- `python3 -m pytest -q`：**69 passed**（含并发、恢复、尾组、越权、篡改、
  分布诊断；慢测试默认一并运行）。
- 覆盖率 `--cov=app`：行覆盖 **94%**（TOTAL 1544 stmts，97 miss），
  高于 80% 下限；未覆盖部分为少量防御分支与 `app.asgi` 薄入口。

### 9.2 复现实验 CLI
`python3 -m app.reproducibility.cli --workdir ./data/repro --out
./data/repro/report.json` → 退出码 **0**，`summary.overall = PASS`：
串行双跑逐对象一致、并发槽位完整、特征篡改被拒且原臂保留、
尾组波次拒绝类别正确、恢复后续跑正确、所有均衡/流复核通过。
完整 JSON 报告见 `data/repro/report.json`。

### 9.3 真实 HTTP 端到端（uvicorn 127.0.0.1:8011，全新数据目录）
用与测试相同的固定种子创建 `DEMO-1`（2 臂 1:1，center×stage 分层，
区组长 4，keep_open），实测：

- 契约指纹 `ctr_3599faac…`；首个对象 `P0(C1|early)` 在 block0/pos0
  得 **treatment**，与独立参考实现的冻结向量一致。
- 5 个不同层对象各自落在本层 block0/pos0；重复请求 `outcome=replayed`、
  返回原臂且 `original_request_id=demo-req-0`。
- 改 `center=C1→C2` 重放：**409 `FEATURES_CHANGED_AFTER_ALLOCATION`**，
  响应同时给出首末特征摘要与原层键；`auditor` 调登记：**403
  `FORBIDDEN_ROLE`**。
- 填满 `C1|early` 一个整组：计数 **control=2 / treatment=2，偏差 0/0**；
  其余 5 个未满区组全部列入 `INCOMPLETE_TAIL_BLOCK`（不确定，不计失败）；
  `verdict=PASS`。
- **分配隐藏**：直接读 SQLite，开放区组 `perm_json IS NULL`
  （`('C1|early',1,1,'open', True)`），排满区组才留档置换。
- 随机流复核 `verdict=PASS`（8 项检查，0 失败）。
- 分布诊断（6 固定种子 × 200 区组，K=4 全 24 置换枚举）：
  χ²=29.04，**p=0.179（未检出偏离）**；逐位置比例未检出；游程 p=0.21；
  trace 明确含 `STATISTICAL_NON_PROOF`（不显著≠证明）。
- 封尾：5 个层的未满尾组被封闭，样例
  `C1|early block1 filled=1/4 missing=3`。
- JSONL 审计日志每行带 `request_id / actor_role / action / outcome /
  spec_version / service_version / contract_fingerprint / steps`。

已知非问题：测试输出中有一条 Starlette 关于 `httpx`/TestClient 的
弃用告警，不影响功能。
