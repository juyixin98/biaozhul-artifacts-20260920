# 多签延时执行器（MultiSig Timelock Executor）

基于 **Solidity + Foundry + Python(FastAPI + web3.py)** 的本地多签延时执行器。
所有链环境只使用本机 **Anvil** 与 Anvil 内置**公开测试密钥**，HTTP 服务仅连接本地链。

## 功能与规则

- **预置签名人与阈值**：合约构造时设定签名人集合与阈值；运行期只能通过钱包自身的多签提案
  （`configureSigners` / `setDelay` 自调用）修改，部署者也无法直接改。
- **操作哈希绑定**：按 EIP-712 对 `(target, data, value, nonce, validUntil)` 求摘要，
  签名只对该操作有效；替换任一字段（如 calldata）签名即失效。
- **签名乱序安全 + 去重**：合约对每份签名独立 `ecrecover`，按签名人地址去重
  （同一签名人对同一操作无论在一笔还是多笔交易里提交，都只计一次），签名顺序任意。
- **达到阈值仍需等待延时**：有效签名数 ≥ 阈值时操作被调度（`Scheduled`），
  记录 `readyAt = now + delay`，早于该时间执行直接回滚。
- **每个操作至多成功执行一次**：目标调用成功后置 `executed` 终态，再执行回滚
  `AlreadyExecuted`。
- **目标失败的明确重试规则**：目标调用失败时**不**回滚执行交易、**不**置终态，
  而是记录 `lastFailureAt` 并发出 `ExecutionFailed(..., nextTryAfter)`；
  下次执行必须满足 `now >= lastFailureAt + retryCooldown`，否则回滚
  `RetryCooldownActive(nextTryAfter)`。冷却后可无限重试直至成功，成功后同样不可再执行。
- **时限与防重放**：`validUntil` 之后不可签名调度/执行；`nonce` 在调度时即消费，
  同 nonce 的任何再提案回滚 `NonceAlreadyUsed`。
- **取消**：调度后、成功前任一签名人可 `cancel`，取消即终态。

> 语义说明：EVM 中子调用 revert 会回滚其自身全部状态变更，因此“目标按自己的计数
> 失败 N 次”在真实链上不可实现。示例目标 `Counter` 用**时间条件**模拟“暂时失败、
> 之后被修复”，这是真实可重试目标（如外部服务恢复、流动性到位）的准确模型。

## 目录结构

```
contracts/
  src/MultiSigTimelock.sol   # 执行器合约
  src/Counter.sol            # 示例/测试目标（含时间条件失败模式）
  test/                      # Foundry 测试（自带极简 Vm/Assert，零外部依赖）
  foundry.toml
app/
  chain.py                   # web3.py：EIP-712 签名、合约封装、日志解码
  main.py                    # FastAPI HTTP 接口
scripts/deploy.py            # 部署到本地 Anvil
tests/                       # pytest 端到端测试（自动起停 Anvil + 部署）
requirements.in / requirements.lock
```

## 依赖

- [Foundry](https://book.getfoundry.sh/getting-started/installation)（提供 `anvil`/`forge`），已验证版本 1.8.3
- Python 3.12（3.10+ 应可运行）
- Python 依赖见 `requirements.lock`（web3 7.x / eth-account 0.14 / fastapi / uvicorn / pytest）

安装 Foundry：

```bash
curl -L https://foundry.paradigm.xyz | bash   # 之后 ~/.foundry/bin 加入 PATH
foundryup
```

安装 Python 依赖（建议虚拟环境）：

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock    # 或 -r requirements.in
```

## 编译与测试

### 1. 合约单元测试（Forge，无需起链）

```bash
cd contracts
forge build
forge test -vv
```

### 2. 端到端测试（pytest：自动启动 Anvil、部署、走 HTTP API）

```bash
# 在仓库根目录；需要 ~/.foundry/bin 下有 anvil
export PATH="$HOME/.foundry/bin:$PATH"
.venv/bin/pytest
```

## 本地启动与示例（手动）

终端 1 —— 启动本地链：

```bash
anvil --port 8545
```

终端 2 —— 编译并部署（预置 3 个 Anvil 测试签名人，阈值 2，延时 3600s，失败冷却 600s）：

```bash
export PATH="$HOME/.foundry/bin:$PATH"
( cd contracts && forge build )
.venv/bin/python scripts/deploy.py            # 写入 deployments.json
```

终端 3 —— 启动 HTTP 服务：

```bash
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
```

下面用一组命令走完整流程。为便于观察“延时”，可把延时时长调短，
或用 anvil 控制台 `evvil_increaseTime` 推进时间：

```bash
BASE=http://127.0.0.1:8000

# 0. 状态
curl -s $BASE/state | python3 -m json.tool

# 1. 提案：对示例 Counter.increment() 发起操作
OP=$(curl -s -X POST $BASE/actions/increment -H 'Content-Type: application/json' -d '{}')
HASH=$(echo "$OP" | python3 -c 'import sys,json;print(json.load(sys.stdin)["op_hash"])')
NONCE=$(echo "$OP" | python3 -c 'import sys,json;print(json.load(sys.stdin)["nonce"])')
echo $HASH

# 2. 收集两个签名人的签名（顺序任意；重复提交同一人不会增加计数）
S1=0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266
S2=0x70997970C51812dc3A010C7d01b50e0d17dc79C8
for S in $S2 $S1; do
  curl -s -X POST $BASE/operations/$HASH/sign -H 'Content-Type: application/json' \
    -d "{\"signer\":\"$S\",\"nonce\":$NONCE,\"validUntil\":0}" | python3 -m json.tool
done

# 3. 提交签名上链 -> 达到阈值，进入调度
curl -s -X POST $BASE/operations/$HASH/approve -H 'Content-Type: application/json' \
  -d "{\"nonce\":$NONCE,\"validUntil\":0}" | python3 -m json.tool

# 4. 延时未到执行 -> 交易回滚 TimelockNotReady
curl -s -X POST $BASE/operations/$HASH/execute -H 'Content-Type: application/json' \
  -d "{\"nonce\":$NONCE,\"validUntil\":0}" | python3 -m json.tool

# 推进链上时间（在另一个终端执行；也可用 curl 调 anvil RPC）
cast rpc evm_increaseTime 3610 >/dev/null; cast rpc evm_mine >/dev/null

# 5. 延时过后执行 -> executed，Counter=1
curl -s -X POST $BASE/operations/$HASH/execute -H 'Content-Type: application/json' \
  -d "{\"nonce\":$NONCE,\"validUntil\":0}" | python3 -m json.tool

# 6. 再次执行 -> 回滚 AlreadyExecuted（至多成功一次）
curl -s -X POST $BASE/operations/$HASH/execute -H 'Content-Type: application/json' \
  -d "{\"nonce\":$NONCE,\"validUntil\":0}" | python3 -m json.tool
```

### 失败重试示例

部署一个“部署后 1 小时内失败、之后恢复”的 Counter：

```bash
.venv/bin/python scripts/deploy.py --counter-fail-until 3600
```

在延时窗之后执行会得到 `"outcome": "failed_retryable"` 与 `next_try_after`；
冷却期内再执行回滚 `RetryCooldownActive`；冷却过后且目标恢复后执行成功。
端到端测试 `test_04_failure_cooldown_retry_success_once` 自动演示了该全过程。

### 签名人变更示例（必须走多签自调用）

```bash
curl -s -X POST $BASE/signers/change -H 'Content-Type: application/json' -d '{
  "signers": [
    "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266",
    "0x70997970C51812dc3A010C7d01b50e0d17dc79C8",
    "0x90F79bf6EB2c4f870365E785982E1f101E93b906"
  ],
  "threshold": 2
}'
# 然后与普通操作一样：旧签名人签名 -> approve -> 等延时 -> execute；
# 变更生效后被移除签名人的签名在链上直接 InvalidSignature。
```

## HTTP 接口一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 链连通性、链 ID、区块高度 |
| GET | `/state` | 链上签名人、阈值、延时、当前时间 |
| POST | `/operations` | 通用提案（target/data/value/nonce/validUntil） |
| POST | `/actions/increment` | 便捷提案 Counter.increment() |
| POST | `/signers/change` | 便捷提案：变更签名人/阈值 |
| POST | `/operations/{hash}/sign` | 指定签名人测试密钥离线 EIP-712 签名 |
| POST | `/operations/{hash}/approve` | 提交收集到的签名上链（合约去重） |
| GET | `/operations/{hash}` | 链上操作状态 + 本地提案/签名 |
| GET | `/operations` | 列出本地提案 |
| POST | `/operations/{hash}/execute` | 到期执行；返回 `executed` / `failed_retryable` |

`execute` 响应中：
- `tx.status = success` 表示执行交易成功（目标失败也不回滚交易）；
- `outcome`：`executed`（目标成功，终态）/ `failed_retryable`（目标失败，可冷却后重试）；
- `next_try_after`：失败后允许重试的最早链上时间戳；
- 若**钱包规则**拒绝（未到延时/冷却中/已执行等），交易回滚，`tx.status=reverted`，
  `tx.error` 含自定义错误名。

## 测试覆盖对照（验收点）

| 验收点 | Foundry | pytest |
|---|---|---|
| 签名乱序 | `test_01_OutOfOrderSignaturesSchedules` | `test_01_..._execute_once` |
| 重复签名去重 | `test_02_DuplicateSignaturesDeduplicated` | `test_02_duplicate_...` |
| 跨交易收集签名 | `test_02b_ApprovalsAcrossTransactions` | `test_03_approve_twice_then_schedule` |
| 延时 + 至多成功一次 | `test_03_TimelockDelayThenExecuteOnce` | `test_01_...` |
| 目标失败按规则重试 | `test_04*` | `test_04_failure_cooldown_retry_success_once` |
| 时限 validUntil | `test_05*` | — |
| nonce 防重放 | `test_06_*` | `test_05_nonce_replay_rejected` |
| 签名人变更 | `test_07_SignerChangeViaSelfCall` | `test_06_signer_change_via_multisig` |
| 执行回调返回数据 | `test_08_ExecuteReturnData` | `test_07_signature_bound_to_calldata`（同时验证哈希绑定） |
| 构造参数校验 | `test_09_ConstructorRejectsBadConfig` | — |

## 实际运行结果（2026-09-24，本机实测）

环境：Linux 6.8 / Python 3.12.3 / Foundry 1.8.3 / web3.py 7.16 / eth-account 0.14。

**合约测试**：`cd contracts && forge test`

```
Suite result: ok. 12 passed; 0 failed; 0 skipped
```

**端到端测试**：`.venv/bin/pytest`（每例自动起停 Anvil、部署合约、走 HTTP API）

```
8 passed, 2 warnings in 16.15s
```

**手动真实演示**（`scripts/demo.sh` + Anvil RPC 推时，原始日志为实际输出）：

- 乱序签名（S3 先、S1 后）+ S3 重复签名：`approvalCount` 先为 1（重复不增加），
  补 S1 后变为 2 并 `scheduled=true`，`readyAt = 调度时间 + 2s`。
- 延时窗内执行：交易回滚 `TimelockNotReady(1790257184)`；推时 +3s 后执行：
  `outcome=executed`，`Executed` 事件返回回调数据 `0x…0001`，Counter=1。
- 再次执行：回滚 `AlreadyExecuted()`，Counter 保持 1（至多成功一次）。
- 签名人变更 `[S1,S2,S3] → [S1,S2,S4]`：旧集合 S2+S3 签名 → 调度 → 延时内
  `TimelockNotReady` → 推时后自调用执行成功；`/state` 显示 S3 已移除、S4 在列。

**失败重试真实演示**（独立 Anvil，Counter 先处于失败窗口，随后用
`setSucceedAfter(0)` 模拟“目标被修复”）：

```
延时窗内执行  -> reverted  TimelockNotReady(...)
到点执行(目标坏) -> tx=success, outcome=failed_retryable, executed=false,
                    counter=0, next_try_after=1790257535
立即重试      -> reverted  RetryCooldownActive(1790257535)
修复目标+推时  -> tx=success, outcome=executed, counter=1
再执行        -> reverted  AlreadyExecuted(), counter=1
```

复现命令见本文件“本地启动与示例”，重试场景可用：

```bash
.venv/bin/python scripts/deploy.py --counter-fail-until 100000   # 失败窗口足够大，便于推时观察
```

## 未完成项 / 限制（如实说明）

- 仅面向本地 Anvil/HTTP，未编写真实网络部署脚本与前端页面。
- 服务端提案/签名为**进程内内存**存储，重启即清空；操作的唯一真相是链上状态，
  内存记录只是把“操作参数→哈希”的对应关系留在服务端方便收集签名。
- 无事件索引与数据库、无鉴权/TLS（本地演示不需要）。
- `execute` 的返回值在失败重试时为空字节；目标失败原因通过 `ExecutionFailed`
  事件的 `returnData`（原始 ABI 错误数据）暴露，API 原样透传 hex，未进一步做
  人类可读解码（成功回调返回数据已原样透传）。
- 合约未做 formal audit；`approve` 入口任何人都可调用（只校验签名内容），
  这是多签钱包的常见设计（提交交易无需特权），但使用方需知悉。

## 安全边界与说明

- 仅用于本地学习/演示：`deployments.json` 内含 Anvil 公开测试密钥，已在 `.gitignore`
  忽略；切勿在任何真实网络使用这些密钥。
- 服务端的提案/签名缓存是进程内内存存储，重启清空；**操作真相始终以链上状态为准**。
- 未做事件索引/数据库持久化、gas 估算优化与正式部署脚本（非本任务目标）。
