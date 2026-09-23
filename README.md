# 支付通道状态裁决 · Payment Channel Adjudicator

一个**纯后端**的单向测试代币（test-token）支付通道与链上裁决器实现，使用
**Solidity + Foundry**。付款方把抵押品锁进合约，双方在链下对「累计支付状态」
做本地签名；上链时由裁决器按序号与累计额规则裁决，进入挑战期，到期一次性结算。

> 本项目是一个独立的教学/测试实现。**它不声称、也不兼容闪电网络（Lightning
> Network）或任何其他既有支付通道协议。** 代币是无价值的测试代币，请勿用于真实资产。

---

## 1. 它做了什么

- **单向支付通道**：`payer`（付款方）开通并存入抵押，`payee`（收款方）收款。
- **链下双方本地签名状态**：每个状态是一条 EIP-712 消息，签名同时绑定
  - `channelId`（通道唯一标识，含链、合约、双方、抵押、时长、nonce），
  - `chainId`（链标识，防跨链重放），
  - `cumulativePaid`（**累计**支付额），
  - `sequence`（单调递增序号）。
  付款方与收款方**都**必须签名，角色固定、不可互换。
- **关闭 → 挑战期 → 结算** 的状态机：
  - `openChannel`：开通道并托管抵押；
  - `close(state, sigPayer, sigPayee)`：用首个双方签名状态关闭，进入挑战期；
  - `startChallenge`：对手离线时发起零额挑战，另一方可在窗口内用签名状态推翻；
  - `challenge(state, ...)`：挑战期内提交**更新**的状态；
  - `settle`：挑战期结束后结算，仅可结算一次。

### 裁决规则（均有真实测试覆盖）

| 规则 | 行为 |
|---|---|
| 更新必须序号更高 | `sequence <= bestSequence` → 回滚 `StaleSequence` |
| 累计额不得回退 | `cumulativePaid < best` → 回滚 `AmountReverted`（即使序号更高） |
| 跨通道重放 | 状态里的 `channelId` 不符 → `WrongChannel`；篡改 id 则签名验不过 |
| 跨链重放 | `chainId != block.chainid` → `WrongChainId` |
| 错误/篡改签名 | 恢复出的地址与角色不符 → `BadPayerSignature` / `BadPayeeSignature` |
| 签名延展性 | 只接受 EIP-2 下半区 `s`，拒绝高 `s` 的孪生签名 |
| 旧状态不得覆盖最新状态 | 挑战期内任何回滚/旧序号提交都被拒绝 |
| 到期最多支出抵押额 | 支付额被截断在 `collateral`，剩余退还付款方 |
| 不得重复结算 | 结算后状态为 `Settled`，再次 `settle` → `AlreadySettled` |
| 挑战期时间边界 | `now < endsAt` 结算被拒；`now >= endsAt` 可结算 |

所有计算（EIP-712 哈希、secp256k1 恢复、金额截断、状态迁移）都在 EVM 中**真实执行**；
所有签名都由 Foundry 的 `vm.sign` 或 `cast wallet sign` 用真实私钥产生，**没有桩、
没有模拟、没有预置地址常量**。

---

## 2. 目录结构

```
src/
  PaymentAdjudicator.sol   # 通道裁决器（EIP-712、状态机、结算）
  Ecdsa.sol                # 自实现 ECDSA 恢复（EIP-2/EIP-2098，仅用 ecrecover 预编译）
  TestToken.sol            # 测试用 ERC-20（可任意 mint，无价值）
  IERC20.sol
lib/forge-vm/              # 手写的 Foundry cheatcode 接口子集（本地源码，非外部依赖）
  Vm.sol  Test.sol  Script.sol  console.sol
test/
  PaymentAdjudicator.t.sol # 37 个端到端/裁决/时间边界测试中的 29 个
  Ecdsa.t.sol              # 8 个 ECDSA 密码学单元测试
script/
  Demo.s.sol               # 零节点、纯内存全流程演示（forge script）
  live-demo.sh             # 真实 Anvil 节点 + cast 真实私钥签名的链上全流程
  sign-state.sh            # 对单个状态做两种真实签名（raw digest / EIP-712 JSON）
examples/
  eip712-typed-data.json   # 标准 EIP-712 TypedData 示例输入
foundry.toml               # 锁定 solc 0.8.26 / evm cancun / via_ir
remappings.txt
```

### 依赖与「锁定」

本项目**不引入任何第三方代码**（没有 git submodule、没有 npm 包），因此不存在
供应链版本漂移：
- Solidity 编译器在 `foundry.toml` 锁定为 **`solc = "0.8.26"`**，EVM 目标
  **`cancun`**，开启 optimizer 与 via_ir；
- 唯一「库」`lib/forge-vm/` 是**本仓库内手写的接口声明**（Foundry cheatcode 的
  固定地址调用），随源码一起版本化；
- Foundry 工具链：本文档基于 **forge/cast/anvil 1.8.3** 验证。

---

## 3. 本地启动（前置）

需要 Foundry。若尚未安装：

```bash
curl -L https://foundry.paradigm.xyz | bash
foundryup
forge --version   # 本文档验证版本: 1.8.3
```

然后编译：

```bash
forge build
```

---

## 4. 验收命令

> 下列命令在仓库根目录执行。若 `forge` 不在 PATH，先
> `export PATH="$PATH:$HOME/.foundry/bin"`。

### 4.1 自动化测试（最快，核心验收）

```bash
forge test -vv
```

预期：**37 passed; 0 failed**。涵盖开通/托管、双方签名校验、签名篡改、跨通道与
跨链重放、序号/累计额单调、旧状态无法覆盖、挑战期时间边界（到期前 1 秒拒绝、
到期当刻成功）、超额抵押截断、重复结算拒绝，以及一个 256 轮的模糊测试。

带 gas 报告：

```bash
forge test --gas-report
```

### 4.2 零节点内存全流程（真实签名 + 真实时间控制）

```bash
forge script script/Demo.s.sol
```

打印旧状态被用于恶意关闭、收款方用最新状态反制、旧序号重放被拒、到期前结算被拒、
到期当刻结算（700 给收款方 / 300 退还 / 合约余额 0）、重复结算被拒。

### 4.3 真实 Anvil 节点 + 真实私钥签名全流程（推荐最终验收）

脚本会**自己拉起一个本地 Anvil**（不需要你手动开节点），部署合约，并用
`cast wallet sign` 对 EIP-712 摘要做真实 secp256k1 签名后上链：

```bash
./script/live-demo.sh
```

也可以对已运行的节点执行：`RPC_URL=http://127.0.0.1:8545 ./script/live-demo.sh`。

成功标志（结尾）：

```
=== LIVE DEMO COMPLETE: newest state won, funds conserved, no double spend ===
```

### 4.4 示例输入 / 自己签一个状态

`examples/eip712-typed-data.json` 是标准 EIP-712 TypedData 文档，字段与合约的
`PaymentState` 一一对应。在运行中的本地节点上：

```bash
./script/sign-state.sh
```

它会分别用「原始摘要 `--no-hash`」与「标准 TypedData JSON `--data --from-file`」
两种方式产生真实签名；二者在 secp256k1 层面是对同一 32 字节摘要签名，链上恢复
出同一地址，均被 `close()/challenge()` 接受（`live-demo.sh` 已实际验证）。

---

## 5. EIP-712 状态格式

```
PaymentState(bytes32 channelId,uint256 chainId,uint64 sequence,uint256 cumulativePaid)

EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)
  name    = "PaymentAdjudicator"
  version = "1"
```

合约公开了 `hashState`、`domainSeparator`、`stateDigest`、`computeChannelId` 视图，
可离线复算摘要并核对，再签名。签名为 65 字节 `r‖s‖v`（同时兼容 64 字节 EIP-2098
紧凑形式）。

---

## 6. 安全模型与注意事项

- **时间依赖**：结算时间用 `block.timestamp`，在 PoS 链上由出块者小幅影响；
  挑战期不宜设得过短（合约强制最短 1 分钟）。
- **挑战期刷新**：每次接受更高状态都会把截止时间顺延一个完整挑战窗口，给对方
  对每个新「最高状态」的反应时间。
- **抵押上限**：任何状态承诺都不可能让合约付出超过托管的抵押；超过部分被截断。
- **双方签名**：付款方/收款方签名按固定角色分别校验，交换或复用对方签名无效。
- **测试代币**：`TestToken` 任何人可 `mint`，仅用于本地测试与演示。
- 本实现聚焦核心裁决逻辑，未包含手续费模型、争议保证金、多跳/路由等功能，
  且**不兼容闪电网络**。

---

## 7. 端到端生命周期速览

```
        openChannel        close(seq=1,旧)        challenge(seq=3,最新)      settle(到期)
None ───────────────▶ Open ───────────────▶ Challenged ───────────────▶ Challenged ─────────▶ Settled
                       escrow collateral      (挑战期倒计时)            (窗口可继续更新)       一次性转账
```
