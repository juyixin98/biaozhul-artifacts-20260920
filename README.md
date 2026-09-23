# 哈希时间锁兑换（HTLC Atomic Swap）演示

使用 **Solidity 0.8.24 + Foundry** 从零实现的哈希时间锁（Hash Time Locked Contract）原子兑换，
纯后端：合约 + 自动化测试 + 驱动**两条独立本地区块链**的回放脚本，无前端。

## 这是什么

双方在两条链上各锁定一笔 ERC-20 测试资产，两把锁共享同一个 **SHA-256 原像**：

```
链 A：Alice 锁定 100 TKA ──指定给──▶ Bob（截止时间 TA，较晚）
链 B：Bob   锁定  50 TKB ──指定给──▶ Alice（截止时间 TB，较早）

1. Alice 在 B 链出示原像领取 50 TKB  ──▶ 原像随 Claimed 事件公开上链
2. Bob   读到原像，在 A 链领取 100 TKA
3. 若无人领取，两把锁各自到期后由锁定人退款
```

- **锁定绑定四要素**：接收者 `receiver`、摘要 `hashLock = sha256(secret)`、金额 `amount`、截止时间 `timelock`。
- **领取**：只有 `receiver` 能调用，必须给出满足 `sha256(secret) == hashLock` 的原像，且 `block.timestamp < timelock`。
- **退款**：只有 `sender` 能调用，且 `block.timestamp >= timelock`。
- **状态机不可逆**：`NONE → LOCKED → CLAIMED 或 REFUNDED`，`LOCKED` 只能进入**一种**终态。
- **安全时间间隔**：`TA = TB + MARGIN`（演示默认 300 秒）。Alice 最晚在 B 链到期前 1 秒领取，
  Bob 仍有完整的 `MARGIN` 秒在 A 链反应；短超时的一侧（Bob 的锁）先到期，风险敞口最小。

> ⚠️ **重要：本项目不保证真实跨链原子性。**
> 两条链只是同一台机器上两个独立的 `anvil` 进程；脚本中“跨链拿到原像”是从收据日志读取的**模拟编排**，
> 不存在真实的中继网络、区块头轻客户端或见证证明。真实部署中还依赖：对手方在线监听日志、
> 链上时间戳与出块时间的偏差、Gas 价格波动、两条链的最终性，以及 `MARGIN` 必须覆盖
> “最坏情况下对端领取被打包 → 本方领取被打包”的完整时延。这里演示的是 HTLC 的**机制与合约不变量**，
> 不是可直接用于主网的跨链产品。

## 目录结构

```
src/
  HashTimeLock.sol          # 哈希时间锁合约（零外部依赖）
  TestToken.sol             # 可公开铸造的本地测试 ERC-20
  IERC20.sol                # 最小 ERC-20 接口
test/
  HashTimeLock.t.sol        # 23 个单元测试（含重复领取/错误原像/恰好到期/重入）
  AtomicSwap.t.sol          # 4 个双链兑换场景测试（同一进程内用两个合约实例模拟两条链）
  helpers/
    MaliciousToken.sol      # 转账时回调的恶意 ERC-20（重入测试）
    ReentrantAttacker.sol   # 重入攻击合约
script/
  Deploy.s.sol              # forge script 部署（单链）
  demo.sh                   # 端到端回放：启动两条 anvil → 部署 → 兑换/退款全过程
  demo.env.example          # 回放脚本的示例输入（金额、超时、安全间隔、原像）
foundry.toml                # Solc 0.8.24 锁定、RPC 别名等
remappings.txt
Makefile
```

## 环境要求

| 工具 | 用途 | 本项目验证版本 |
|---|---|---|
| forge / cast / anvil | 编译、测试、交易、本地链 | Foundry **v1.8.3**（solc 0.8.24 由 forge 自动下载） |
| bash 4+、jq、openssl、xxd | 回放脚本 | Linux 常规自带 |

安装 Foundry（如尚未安装）：

```bash
curl -L https://foundry.paradigm.xyz | bash
foundryup
export PATH="$PATH:$HOME/.foundry/bin"
```

### 依赖锁定

唯一外部依赖 `forge-std` 以 **git submodule 固定到精确提交 `7239323e`**，
solc 版本在 `foundry.toml` 锁定为 **0.8.24**（另有 `foundry.lock`）。全新克隆后恢复：

```bash
git submodule update --init --recursive
forge build     # 首次构建会自动下载锁定版本的 solc
```

## 快速开始（验收命令）

```bash
# 1) 编译
make build            # 等价 forge build

# 2) 全部自动化测试（27 个：单元 + 双链场景，毫秒级完成，无需起链）
make test             # forge test -vv
make test-gas         # 附带 gas 报告

# 3) 端到端回放（自动起两条 anvil 链、部署、跑完整流程后自动关链）
make demo             # bash script/demo.sh
```

`make demo` 会依次真实执行并打印 `[OK]`：

1. 用 `openssl rand -hex 32` 生成随机原像，`openssl sha256` 计算摘要（真实密码学操作）；
2. 启动链 A（chainId 31337，:8545）与链 B（chainId 31338，:8546）；
3. 两条链分别真实部署 `TestToken` 与 `HashTimeLock`（真实交易、真实收据）；
4. 双方锁定 → 错误原像/非接收者/未到期退款均链上回滚；
5. Alice 在 B 链领取，脚本**从 B 链收据的 `Claimed` 日志中解码出原像**，并重新算 SHA-256 比对；
6. Bob 用该原像在 A 链领取成功，重复领取/终态后退款均回滚；
7. 场景二：两把新锁均不领取，`evm_increaseTime` 推进链上时钟到**恰好到期**——
   领取失败、仅发送者退款成功，终态后再领取回滚；
8. 汇总两链四把锁的终态。

退出码为 0 即全部通过；任一步骤与预期不符脚本立即非零退出（`[FAIL]`）。

## 示例输入

```bash
cp script/demo.env.example script/demo.env   # 可选；不改也能用内置默认值
```

可调参数（见 `script/demo.env.example`）：

| 参数 | 默认值 | 含义 |
|---|---|---|
| `AMOUNT_A` / `AMOUNT_B` | 100 / 50（×1e18） | 双向锁定金额 |
| `SECONDS_TO_TB` | 120 | B 链锁相对当前时间的秒数 |
| `SAFETY_MARGIN` | 300 | 安全间隔：`TA = TB + 300s` |
| `SECONDS_TO_TB2` / `SAFETY_MARGIN2` | 40 / 120 | 退款场景的两个超时 |
| `SECRET_HEX` | 留空 | 固定原像复现实验；留空则每次随机生成 32 字节 |

## 手动单链部署（可选）

```bash
make anvil-a                      # 终端 1：起链 A
make deploy-a                     # 终端 2：部署 TokenA + HashTimeLock
```

## 测试清单

`test/HashTimeLock.t.sol`（23 个）：

- 锁定四要素全部落链、资金进入托管、lockId 单调递增；
- 零地址/零金额/截止时间不在未来的锁定均回滚；
- 截止前正常领取；**重复领取回滚**；**错误原像、空原像、篡改长度原像回滚**；
- **时间恰好到期**：`block.timestamp == timelock` 时领取回滚（严格 `<`），退款成功（`>=`）；
- 到期前退款回滚、重复退款回滚、非发送者退款回滚、非接收者领取回滚、未知 lockId 回滚；
- `CLAIMED` 与 `REFUNDED` 互为终态，谁都不能再把锁推到另一终态；
- **重入**：用转账即回调的恶意 ERC-20 + 攻击合约，在 `claim` / `refund` 转账过程中重入，均被
  `nonReentrant` 拒绝，资金只支付一次（合约同时遵循 Checks-Effects-Interactions，先置终态再转账）。

`test/AtomicSwap.t.sol`（4 个，用两个 HTLC 实例模拟两条独立链）：

1. 完整happy path：Alice B 链领取 → 从日志提取原像 → Bob A 链领取，余额正确；
2. 放弃兑换：到期前都不能退，B 先退、A 后退，原像从未上链；
3. 安全间隔：Alice 卡在 B 到期前 1 秒领取，Bob 在 A 到期前 1 秒仍能领取；
4. 反面教材：Bob 拖过 A 的截止时间则领取失败、资金被 Alice 退款——说明 `MARGIN` 的必要性。

## 设计与安全说明

- **时间边界**采用与多数 HTLC 一致的半开区间语义：领取要求严格 `t < timelock`，退款要求 `t >= timelock`，
  不存在“同一时刻两种操作都可行”的歧义。测试专门覆盖 `t == timelock`。
- **原像长度不做限制**（仅非空）：锁的是 32 字节摘要，原像可以是任意字节串，合约校验的是摘要相等。
- **防重入双保险**：`nonReentrant` 互斥锁 + 状态先于外部调用落库。
- 自定义错误（custom errors）+ 命名回滚原因，gas 低且事件可审计；`Claimed` 事件携带原像，
  这是协议的有意设计——原像本就必须公开，对端据此在另一条链领取。
- `block.timestamp` 由验证者出块时给出，真实链上只能近似“墙上时钟”；这也是 `MARGIN` 必须留足的原因之一。
- 测试代币 `TestToken` 任何人可铸造，**无任何价值**，禁止用于正式环境。

## 免责声明（再次）

本仓库仅用于教学与本地验证。回放脚本里的“跨链”是单机编排，不含跨链消息传递与证明系统；
合约未经审计。时间锁只解决“出示同一原像”这一层的原子性，真实跨链原子性还取决于网络、中继、
最终性与超时参数，本项目不对任何资金损失负责。
