# 多签延时执行器（Multisig Timelock Executor）

一个完全运行在**本机 Anvil** 上的多签 + 延时执行系统：

- **链上合约**：Solidity `MultisigTimelock`（Foundry 工程，零第三方 Solidity 依赖）
- **HTTP 服务**：Python + FastAPI + web3.py，连接本地 Anvil RPC
- **签名**：EIP-712 离线签名，签名内容绑定 `target / value / dataHash / nonce / deadline / configVersion`
- **执行模型**：达到阈值后进入 `Scheduled`，必须再等待一个 timelock 窗口，任何人都可触发执行
- **执行保证**：每个操作至多成功执行一次；目标失败按明确的重试规则处理

> ⚠️ 仅用于本机测试。所有私钥均为 Anvil 公开测试密钥，切勿用于真实网络。

---

## 1. 目录结构

```
.
├── contracts/
│   ├── MultisigTimelock.sol   # 核心合约
│   └── Targets.sol            # 测试目标：Counter / FlakyTarget / AlwaysFail / ReentrantTarget
├── test/
│   └── MultisigTimelock.t.sol # 16 个 Foundry 合约级验收测试
├── service/
│   ├── app.py                 # FastAPI（uvicorn --factory 启动）
│   ├── chain.py               # web3.py 封装：部署、发交易、Anvil 时间控制
│   ├── timelock.py            # 业务服务层
│   ├── signing.py             # EIP-712 签名（Op / SetSigners）
│   ├── deploy.py              # 部署脚本（写 deployments.json）
│   ├── demo.py                # 一键端到端 HTTP 演示（自启 Anvil + API）
│   └── config.py              # 路径与 Anvil 测试密钥
├── tests/                     # 13 个 pytest HTTP 集成测试（自动启动 Anvil）
├── foundry.toml
├── requirements.in            # 直接依赖
├── requirements.txt           # 完整锁定依赖（pip freeze，62 个包）
└── Makefile
```

---

## 2. 依赖

| 组件 | 版本（已验证） |
|---|---|
| Foundry（forge/anvil/cast） | 1.8.3（`~/.foundry/bin`，或在 PATH 中） |
| Solidity | 0.8.24（foundry.toml 固定） |
| Python | 3.12.3（venv） |
| fastapi | 0.115.6 |
| uvicorn[standard] | 0.34.0 |
| web3.py | 6.20.3 |
| eth-account | 0.11.3 |
| httpx | 0.28.1 |
| pytest | 8.3.4 |

完整传递依赖见 `requirements.txt`（已锁定）。Solidity 侧测试库 `forge-std v1.9.7`
（`lib/forge-std`，用 `--no-git` 安装；缺失时执行
`forge install foundry-rs/forge-std@v1.9.7 --no-git`）。

### 准备环境

```bash
# 1) Foundry（本机已安装于 ~/.foundry/bin）
export PATH="$HOME/.foundry/bin:$PATH"

# 2) Python 虚拟环境 + 锁定依赖
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

# 3) 编译合约
forge build
```

---

## 3. 启动命令（三个终端）

```bash
export PATH="$HOME/.foundry/bin:$PATH"
. .venv/bin/activate

# 终端 A：本地链
anvil --chain-id 31337            # 默认 RPC http://127.0.0.1:8545，预置 10 个测试账户

# 终端 B：部署（初始签名人 = Anvil 前 3 个账户，阈值 2，timelock=2s，
#         失败上限=2，重试冷却=1s）；写 deployments.json
python -m service.deploy

# 终端 C：HTTP 服务（默认 127.0.0.1:8000；若端口被占用用 PORT=8123）
uvicorn service.app:create_app --factory --host 127.0.0.1 --port 8000
# 交互文档：http://127.0.0.1:8000/docs
```

可选环境变量：`RPC_URL`、`CHAIN_ID`、`DEPLOYMENTS_PATH`、`SIGNER_KEYS`（逗号分隔）、
`OPERATOR_KEY`（付 gas 的账户，默认可不是签名人）、`PORT`、`ENABLE_DEV_ENDPOINTS`。

**一键演示**（自动启动 Anvil、部署、起服务并跑完所有验收场景，无需手动开三个终端）：

```bash
python -m service.demo
```

---

## 4. 核心设计

### 4.1 两种不同的哈希

| | 包含字段 | 用途 |
|---|---|---|
| **op id** `opId(target,value,dataHash,nonce,deadline)` | 操作内容 | 合约存储键，跨签名人变更保持稳定，便于查询与终态判定 |
| **签名 digest** EIP-712 `Op(...)` | 上述字段 **+ configVersion** | 签名人实际签名的内容，仅对当前签名人集合有效 |

因此篡改 target 或 calldata 会得到不同的 op id（`UnknownOp`，永不执行）；
用旧签名人集合的私钥为新集合签名会在验签阶段被拒绝（`BadSignature`）。

### 4.2 状态机

```
                达到 threshold 个不同签名人        timelock 到期 + 目标调用成功
 None ─propose→ Proposed ───────────────────→ Scheduled ───────────────────→ Executed（终态）
                   │                              │  │
                   │                              │  ├─ 目标失败（< maxFailures）→ 留在 Scheduled，
                   │                              │  │   failures+1，冷却后可重试
                   │                              │  └─ 第 maxFailures 次失败 → Failed（终态）
                   │                              └─ deadline 后执行 → revert DeadlinePassed，
                   │                                 任何人可调 voidExpired 标为 Void（终态）
                   └────────────────── 签名人集合变更 → Invalidated（终态）
```

### 4.3 规则细则

- **签名去重**：按 `ecrecover` 出的地址去重。同一批次或跨批次重复签名 → `DuplicateSignature`；
  非签名人 → `BadSignature`；签名顺序无关（乱序提交照常计票）。
- **阈值与延时**：合约里达到 `threshold` 个不同签名人后变 `Scheduled` 并记录 `approvedAt`，
  在 `approvedAt + timelockSeconds` 之前执行一律 `TimelockActive`。
- **nonce**：提案时必须等于链上当前 nonce（`NonceUsed`）；达到阈值（调度成功）时
  占用该 nonce。签名人变更本身也是一个占用 nonce 的治理操作。
- **时限 deadline**：绝对 Unix 时间戳。过期后不可执行，只能标记为 `Void`。
- **至多成功一次**：成功即进入终态 `Executed`，之后任何重复提交（同样的 id）都
  `AlreadyTerminal`；目标合约的 `callCount` 在测试中断言恒为 1。
- **失败重试规则（明确）**：
  1. 目标调用用底层 `call` 捕获，失败**不会**回滚外层交易；操作留在 `Scheduled`，`failures += 1`；
  2. 首次执行门禁 = `approvedAt + timelockSeconds`；之后每次重试门禁 =
     `lastAttemptAt + retryCooldownSeconds`，提前重试 → `RetryCooldownActive`；
  3. 累计失败达到 `maxFailures` → 终态 `Failed`，之后**不可再重试**（即使目标随后恢复）；
  4. 任一次成功 → 终态 `Executed`，不可再次执行。
- **签名人变更**：`setSigners` 需要当前集合的 `threshold` 个有效签名，`configVersion += 1`，
  占用一个 nonce；所有在途（未终态）操作立即变为终态 `Invalidated`——旧签名无法调度或执行
  它们，必须用新 nonce 重新提案、重新凑齐签名、重新走完 timelock。
- **防重放/防 malleability**：拒绝 high-s 非规范签名（`s > secp256k1n/2`）；EIP-712
  domain separator 绑定 `chainId` 与合约地址。
- **重入**：`execute` 带 `nonReentrant`；重入目标（`ReentrantTarget`）的嵌套 execute 被拒绝。
- **执行回调**：目标合约被调用时 `msg.sender` 即执行器合约本身（演示中 Counter 记录
  `lastCaller` 与 `callCount`）。

---

## 5. HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 链连接状态 |
| GET | `/state` | 签名人、阈值、configVersion、当前 nonce、timelock 参数 |
| GET | `/targets` | 已部署目标合约地址 |
| GET | `/operations/digest?target=&value=&data=&nonce=&deadline=` | 返回 op id 与待签名 digest |
| GET | `/operations/{op_id}` | 操作快照（状态、failures、readyAt 等） |
| POST | `/operations/propose` | 提案并附带签名（一次到位，也可只提交部分） |
| POST | `/operations/approve` | 对已存在操作追加签名 |
| POST | `/operations/execute` | timelock 后执行；目标失败时返回 `failed_retryable` |
| POST | `/operations/{op_id}/void` | 将过期操作标记为 Void |
| POST | `/admin/signers` | 更换签名人集合/阈值（需当前阈值签名） |
| POST | `/dev/warp` | （仅 Anvil）快进链上时间 `{"seconds": n}` |
| POST | `/dev/flaky` | （演示）切换 FlakyTarget 失败/恢复 `{"failing": bool}` |

签名有两种提供方式：`signer_indices`（用服务钱包内的 Anvil 测试密钥本地签名，顺序任意）
或 `signatures`（外部按 `/operations/digest` 离线签好后提交 65 字节原始签名）。

链上 revert 统一返回 `400 {"detail": {"message": ..., "revert": "ErrorName(...) 0x.."}}`，
custom error 的 selector 会被解码为错误名。

### curl 示例

```bash
# 提案（乱序签名人 2、0），calldata 用 cast 构造
DATA=$(cast calldata "increment(uint256,bytes32)" 7 0x6f6e6365)
DEADLINE=$(( $(date +%s) + 3600 ))
COUNTER=$(python3 -c "import json;print(json.load(open('deployments.json'))['targets']['counter'])")
curl -s localhost:8000/operations/propose -H 'content-type: application/json' -d "{
  \"target\": \"$COUNTER\", \"data\": \"$DATA\", \"deadline\": $DEADLINE,
  \"signer_indices\": [2,0] }"

# timelock 期间执行 -> 400 TimelockActive
sleep 2
curl -s localhost:8000/operations/execute -H 'content-type: application/json' -d "{
  \"target\": \"$COUNTER\", \"data\": \"$DATA\", \"nonce\": 0, \"deadline\": $DEADLINE }"
```

---

## 6. 自动化测试

```bash
# 合约级（16 个，含乱序/重复/non-signer/签名人变更/回调/重试/耗尽/重入/high-s）
forge test -vv

# HTTP 集成（13 个；pytest 自动在空闲端口启动 Anvil，每个用例重新部署）
pytest

# 一键 HTTP 演示（11 项断言）
python -m service.demo
```

### 实际运行结果（2026-09-24，本机真实执行）

- `forge test`：**16 passed / 0 failed**
- `pytest`：**13 passed / 0 failed**（3 个文件：验收 8、重试 2、签名人/外部签名 3）
- `python -m service.demo`：**11/11 checks passed**

合约编译有 4 条 forge lint 提示（`require/revert inside a loop`，出现在验签去重、
签名人校验等合理位置），不影响编译与测试。

---

## 7. 验收点对照

| 验收要求 | 覆盖位置 |
|---|---|
| 签名乱序 | forge `test_unorderedSignatures_schedules`；pytest `test_unordered_signatures_*`；demo 第 2 节 |
| 重复签名（批内/跨批） | forge `test_duplicateSignatureInBatch/AcrossCalls`；pytest 同名用例 |
| 非签名人/伪造签名 | forge `test_nonSignerSignature_reverts`、`test_highSSignature_reverts`；pytest `test_signature_from_non_signer_rejected` |
| 签名人变更 | forge `test_changeSigners_*`；pytest `test_signers.py`；demo 第 4 节 |
| 执行回调（msg.sender 为执行器） | forge `test_executesExactlyOnce_afterTimelock`（断言 `lastCaller`） |
| 每个操作至多成功执行一次 | forge/pytest 多处二次执行断言 + Counter.callCount==1；重入用例 |
| 目标失败按明确规则重试 | forge `test_failureThenRetry_*`、`test_retriesExhausted_*`；pytest `test_retry.py`；demo 第 3 节 |
| 操作哈希绑定目标与参数 | forge `test_executionBindsTargetAndData`；pytest `test_tampered_calldata_is_unknown_op` |

---

## 8. 范围与未完成项（如实记录）

已完成：上述全部功能、锁定依赖、两层自动化测试、一键演示与本文档。

刻意简化 / 未做（本机演示用途，生产化前需要补齐）：

1. **无 ETH 转账的端到端用例**：合约支持 `value`（uint96）且有 `receive()`，但
   HTTP 自动化测试只覆盖了 `value=0` 的合约调用；手工可构造带 value 的操作。
2. **无鉴权 / HTTPS / 速率限制**：HTTP 服务假设只监听本机环回地址。
3. **私钥在内存中**：服务用 Anvil 测试密钥直接签名；生产应接 HSM/硬件钱包，外部签名
   流程（`/operations/digest` + `signatures`）已为此预留。
4. **签名人变更即时生效，本身不再套一层 timelock**（与普通操作不同，见合约注释）；
   若需要“治理操作也延时”，可复用现有调度结构扩展。
5. **被 Invalidated 的操作不能原样复活**：必须用新 nonce 重新提案（设计如此，旧 nonce
   已被占用）；`_liveOps` 历史不做链外分页查询，只有长度视图。
6. Python 侧未做 mypy 严格类型检查与 CI 配置；`forge coverage` 未纳入默认命令。
