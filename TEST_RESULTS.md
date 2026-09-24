# 测试结果与未完成项（如实记录）

- **运行时间**：2026-09-24
- **机器**：Linux 6.8 x86_64
- **工具链**：Foundry v1.8.3（solc 0.8.26, EVM cancun, optimizer 200）、Python 3.12.3、
  web3 7.6.0、fastapi 0.141.1、pytest 9.1.1
- **链**：本机 Anvil，chainId 31337，HTTP `http://127.0.0.1`（开发 8545 / 自测夹具 8555）

## 1. Foundry 测试：15 / 15 通过

命令：`forge test --fuzz-runs 1000`（不变量固定 256 runs）。退出码 0。

### MaliciousReentrancy.t.sol（2/2）
- `test_deposit_reentrancyBlocked` —— 资产代币 `transferFrom` 回调中重入存款，被
  重入锁以自定义错误 `Reentrancy` 拒绝；最终只铸造一次份额、只发生一次资产转入。
- `test_redeem_reentrancyBlocked` —— 资产代币 `transfer` 回调中重入赎回，被拒绝；
  只有外层赎回销毁份额。

### ShareVault.t.sol（12/12，6 个有界模糊每个 1000 runs）
- `test_firstDeposit_locksDeadShares`
- `testFuzz_firstDeposit_revertsWhenTooSmall(uint96)`（1000 runs）
- `test_tinyDeposit_zeroShares_reverts_andKeepsAssets`
- `testFuzz_depositRoundsDown(uint256,uint256)`（1000 runs）
- `testFuzz_redeemRoundsDown(uint256,uint256)`（1000 runs）
- `testFuzz_singleUserRoundTripDust(uint256,uint256)`（1000 runs）
- `testFuzz_fullRedeem_leavesDeadShares(uint96)`（1000 runs）
- `test_firstDonationInflationAttack_isUnprofitable`
- `test_donationToEmptyVault_mintsNoShares`
- `test_deposit_slippageReverts`
- `test_redeem_slippageReverts`
- `testFuzz_twoDepositors_solvencyAfterRedemptions(uint256,uint256,uint256)`（1000 runs）

### ShareVault.invariant.t.sol（状态不变量，256 runs / 128,000 次动作，0 非预期回滚）
Handler 在 4 个参与者间随机执行 deposit / redeem / donate / transferShares，全程满足：
- `invariant_collectiveSolvency`：`Σ floor(shareᵢ·A/T) ≤ A`（所有份额可兑资产之和不超过库内资产）
- `invariant_deadSharesPermanentlyLocked`：初始化后 `balanceOf(address(1)) == 1000` 恒定、供给不低于死份额
- `invariant_rateNeverDecreases`：定点汇率 A/T 单调不减
- `invariant_supplyConservation`：当前供给 + 累计销毁 = 累计铸造 + 1000 死份额

动作分布（一次运行）：deposit 32,070 / donate 32,083 / redeem 31,934 / transferShares 31,913。

### 首发捐赠攻击实测数字（`test_firstDonationInflationAttack_isUnprofitable`）
```
attacker donated : 1000000.000000000000000000   # 攻击者捐赠 100 万枚
attacker recovered: 999.001996007984031937      # 其 1 份额最终只收回约 999 枚
victim recovered : 999.001996007984031937       # 受害者存 1000 枚
victim loss      : 0.998003992015968063         # 损失 0.0998% = 1000/1001，与捐赠额无关
```
攻击者总投入 = 1001 wei 首存 + 1,000,000 枚捐赠，净亏约 999,002 枚；捐赠放大不了其
1 份额的索取权（死份额占走 1000/1001 的库产）。

## 2. Python 集成测试：12 / 12 通过

命令：`pytest`（约 13.5s）。夹具在 8555 端口自管理 anvil，每用例重新部署。

HTTP（FastAPI TestClient，真实链）：
- `test_health_ok`
- `test_vault_info_initial_empty`
- `test_full_flow_via_http`（水龙头→授权→预览→首存→信息→全额赎回，死份额损失恰为 1000）
- `test_slippage_protection_reverts_http`（min_shares 抬高 1 wei 即回滚）
- `test_account_view_balances`
- `test_invalid_address_returns_400`

链上（web3.py 直发交易）：
- `test_first_deploy_is_empty`
- `test_first_deposit_locks_dead_shares`
- `test_deposit_and_redeem_round_trip`
- `test_tiny_deposit_reverts_zero_shares`（999 wei、汇率 1000:1 → ZeroShares，资产不移动）
- `test_donation_to_empty_vault_mints_no_shares`
- `test_slippage_floor_integer_arithmetic`（滑点公式的边界取整）

## 3. 端到端示例（`scripts/demo.py`，实际输出）

```
Alice 首存 5000 枚 → 份额 5000e18 - 1000，死份额 1000，T=A=5000e18（1:1）
捐赠约 499.5 万枚后汇率 ≈ 1000:1
Bob 存 999 wei      → 回滚 0x9811e0c7（ZeroShares），余额不变
Bob 存 1234.000...007 枚（带 7 wei 零头，0.5% 滑点）
                    → preview/实际份额均为 1234e18（7 wei 向下取整留库）
Bob 全额赎回        → 收到 1234e18；库内 T=5000e18、A=5000e18+7 dust
```

HTTP 服务（uvicorn :8000）手工 curl 验证：`/health`、`/vault/info`、`/token/mint`、
`/token/approve`、`/vault/preview/deposit`、`/vault/deposit`、`/vault/redeem` 均返回
预期整数结果；全额赎回后 `total_assets=total_shares=1000`（只剩死份额）；把
`min_shares` 设为不可能的高值时返回 400 且错误体含自定义错误选择子 `0x71c4efed`
（SlippageExceeded）。

## 4. 复现命令

```bash
make install
make build
make forge-test          # 无需 anvil
make test                # 自动管理 8555 端口的 anvil
# 手工端到端：
make anvil               # 终端 A
make deploy && make api  # 终端 B
make demo                # 终端 C
```

## 5. 未完成项 / 已知边界（如实列出）

1. **死份额的固有成本**：方案选择 Uniswap V2 式永久死份额，首次存款者承担
   `1000` wei 的固定损失，且紧随最小首存（1001）的存款者相对损失可达
   `1000/1001 ≈ 0.0998%`。这是有意的安全取舍（换捐赠攻击无利可图），不是 bug；
   生产中可通过“最小首存金额”部署策略压低该比例。没有实现“虚拟份额抵消 /
   ERC-4626 虚增 offset”那类替代方案。
2. **未做手续费型 /  rebasing / 缺精度（deflationary）资产**：资产限定为无手续费、
   18 位的 `MockERC20`。恶意回调只测试了重入，没有覆盖转账金额与申报不符的
   收费代币（合约直接以 `balanceOf` 为准，理论上对收费代币也安全，但未测）。
3. **后端用请求体传私钥**：仅适合本机演示；没有接入托管签名、密钥库或 TLS。
   HTTP 服务仅监听 127.0.0.1。
4. **JSON 金额以 number 传输**：web3/pydantic 侧是 Python 任意精度 int，但标准
   JSON 消费方若用 JS Number 解析超过 2^53 会丢精度；README 已标注最小单位整数，
   未额外提供字符串/十进制刻度转换接口。
5. **CI 配置未编写**：提供了 Makefile 与锁定依赖，但没有附带 GitHub Actions 等
   云端流水线配置（需求只要求本机可复现的自动化测试）。
6. **静态分析告警**：`forge build` 对 `asset.transferFrom/transfer` 报
   reentrancy lint 告警；这是因为分析器不识别本合约手写的 `locked` 重入锁。
   已用恶意代币重入测试证明安全性，未改写为 OpenZeppelin ReentrancyGuard 以保持
   零外部 Solidity 依赖（forge-std 仅测试期使用）。
7. **未做 gas 优化与正式审计**：未引入 ERC-4626 完整接口（如 `mint`/`withdraw`
   反向取整、`maxDeposit` 等），只实现需求要求的 deposit/redeem 核心路径。
