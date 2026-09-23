# 支付通道状态裁决（Payment Channel Adjudication）

单向测试代币支付通道的纯后端实现：Solidity + Foundry。付款方锁定抵押代币，
双方**链下**对累计状态共同签名，链上合约只负责在发生分歧时裁决：
关闭 → 挑战期 → 到期结算。

> 本项目是独立的单向支付通道实现，**不实现、也不声称兼容闪电网络（Lightning Network）协议**。

## 设计

- **状态绑定**：每份链下状态经 EIP-712 签名，绑定
  - 通道 ID（`channelId = keccak256(payer, payee, salt)`）
  - 链 ID 与合约地址（EIP-712 域分隔符）
  - 累计支付额 `amount`（非增量）
  - 单调序号 `nonce`
  一份有效状态必须同时持有 payer 与 payee 两方签名。
- **关闭**：任一方提交一份双方签名状态调用 `closeChannel`，进入挑战期
  （默认 1 天，部署时可配）。提交的状态可以是旧状态。
- **挑战**：挑战期内任何人可提交**序号更高且累计额不回退**的双方签名状态
  覆盖链上记录（`challenge`），不延长挑战期。
- **结算**：挑战期到期后 `settle` 执行一次：收款方获得
  `min(amount, collateral)`，余款退还付款方。重复结算被拒绝。
- **防护**：签名错误、签名对调、跨通道重放、跨链重放、旧状态覆盖新状态、
  累计额回退、挑战期外挑战、挑战期内结算、重复结算，全部回退
  （见测试）。签名验证带低 s 值检查，防可延展性。

## 目录

```
src/PaymentChannel.sol   裁决合约
src/TestToken.sol        最小 ERC20 测试代币（TT）
script/Deploy.s.sol      部署 + 铸币，地址写 deployments/local.json
script/Open.s.sol        开通通道，channelId 写 examples/channel.json
script/Update.s.sol      链下更新：双方签名新状态 → examples/state-signed.json
script/ChannelOps.s.sol  Close / Challenge / Settle 三个链上操作
test/PaymentChannel.t.sol  21 个自动化测试（含时间边界）
examples/state-1.json    示例输入：nonce=1, 30 TT
examples/state-2.json    示例输入：nonce=2, 70 TT
scripts/demo.sh          完全本地的全流程演示
```

## 环境与锁定依赖

- Foundry 工具链（`forge` / `cast` / `anvil`）
- solc **0.8.30**（`foundry.toml` 中 `solc = "0.8.30"` 锁定，首次构建自动下载）
- forge-std **v1.16.2**，锁定于提交 `ad89729373aeac322997fc5f480993db9131d1cd`
  （位于 `lib/forge-std`，随仓库提供，无需联网安装）

## 本地启动

```bash
# 1. 编译
forge build

# 2. 运行全部自动化测试（含时间边界测试）
forge test -vv

# 3. 一键本地演示（自动启动 anvil，跑完整流程并打印最终余额）
bash scripts/demo.sh
```

演示流程：部署 → 开通（抵押 100 TT）→ 链下签名状态 nonce=1(30 TT) 与
nonce=2(70 TT) → **用旧状态 nonce=1 关闭** → 收款方用 nonce=2 **挑战** →
快进 90000 秒 → **结算**。最终余额应为 payee = 70 TT、payer = 999930 TT。

## 手动验收命令（分步）

```bash
# 终端 A：启动本地链
anvil

# 终端 B：
export RPC_URL=http://127.0.0.1:8545

forge script script/Deploy.s.sol --rpc-url $RPC_URL --broadcast
forge script script/Open.s.sol   --rpc-url $RPC_URL --broadcast

# 链下更新（签名不进链）：先签 nonce=1，再签 nonce=2
cp examples/state-1.json examples/state-input.json
forge script script/Update.s.sol --rpc-url $RPC_URL
cp examples/state-signed.json examples/state-1-signed.json
cp examples/state-2.json examples/state-input.json
forge script script/Update.s.sol --rpc-url $RPC_URL
cp examples/state-signed.json examples/state-2-signed.json

# 用旧状态（nonce=1）关闭，进入挑战期
cp examples/state-1-signed.json examples/state-signed.json
forge script script/ChannelOps.s.sol:Close --rpc-url $RPC_URL --broadcast

# 挑战：更高序号状态（nonce=2）覆盖旧状态
cp examples/state-2-signed.json examples/state-signed.json
forge script script/ChannelOps.s.sol:Challenge --rpc-url $RPC_URL --broadcast

# 快进到挑战期结束后结算
cast rpc evm_increaseTime 90000 --rpc-url $RPC_URL
cast rpc evm_mine --rpc-url $RPC_URL
forge script script/ChannelOps.s.sol:Settle --rpc-url $RPC_URL --broadcast

# 验证余额（payee 应得 70e18）
TOKEN=$(grep -o '"token": *"0x[0-9a-fA-F]*"' deployments/local.json | grep -o '0x[0-9a-fA-F]*')
cast call $TOKEN "balanceOf(address)(uint256)" 0x70997970C51812dc3A010C7d01b50e0d17dc79C8 --rpc-url $RPC_URL
```

默认使用 anvil 内置账户：#0 为付款方、#1 为收款方。可用环境变量覆盖：
`PAYER_KEY` / `PAYEE_KEY` / `PAYEE` / `COLLATERAL` / `SALT` / `CHALLENGE_PERIOD` /
`CLOSER`（`payer` 或 `payee`，Close 的提交方）。

## 测试覆盖（`forge test`）

| 类别 | 用例 |
| --- | --- |
| 开通 | 抵押锁定、重复 salt 拒绝、零抵押拒绝 |
| 关闭 | 进入挑战期、非参与方拒绝、签名错误拒绝、签名对调拒绝、nonce=0 拒绝、跨通道重放拒绝、跨链重放拒绝 |
| 挑战 | 更高序号接受、序号相同/更低拒绝（旧状态不得覆盖）、累计额回退拒绝 |
| 时间边界 | 截止前 1 秒可挑战/不可结算；到达截止时刻不可挑战/恰好可结算 |
| 结算 | 支出封顶于抵押额、余款退还、重复结算拒绝、未关闭时拒绝 |
| 全流程 | 旧状态关闭 → 新状态挑战 → 按最新状态结算 |

所有计算、协议流转与签名密码操作均真实在本地 EVM（anvil / forge test）
执行，无任何模拟或跳过。
