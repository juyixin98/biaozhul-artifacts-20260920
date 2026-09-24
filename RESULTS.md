# 实际运行结果

记录环境、测试与演示的真实输出。日期：2026-09-24。

## 环境

- OS：Linux 6.8.0（Ubuntu 24.04，python 3.12.3）
- Foundry：**v1.8.3**（forge / anvil / cast；solc 0.8.26 由 forge 自动下载）
- Python 依赖：`.venv`，按 `requirements.txt` 安装，完整锁定见 `requirements.lock`
  （web3 7.9.0 / fastapi 0.115.6 / uvicorn 0.34.0 / pydantic 2.10.4 /
  eth-abi 5.1.0 / eth-account 0.13.4 / pytest 8.3.4）
- 链：仅本机 Anvil，测试私钥为 Anvil 公开账户 #0
  （`0xac09…ff80`，**仅限本地**）。

## 一、合约测试（Foundry，内置 EVM）

命令：`forge test -vv`

结果：**20 passed, 0 failed, 0 skipped**

```
test_claim_Single                                  单项领取成功，位图/余额正确
test_claim_DuplicateIndex_Reverts                  跨交易重复索引 → AlreadyClaimed
test_claim_WrongAmount_Reverts                     改金额 → InvalidProof，未置位
test_claim_WrongAccount_Reverts                    改账户 → InvalidProof
test_claim_ForgedProof_Reverts                     拿别的索引证明顶替 → InvalidProof
test_claim_CrossContractReplay_Reverts             跨合约重放：实例 A 的证明在 B 被拒
test_claim_CrossChainReplay_Reverts                chainId 改变后 → WrongChain 回滚
test_claimBatch_Success                            批量领取成功
test_claimBatch_InvalidItem_RevertsAll             中间项金额被改 → 整笔回滚
test_claimBatch_DuplicateIndexInsideBatch_RevertsAll  批内重复索引 → 整笔回滚
test_claimBatch_DuplicateAcrossTx_Reverts          跨交易重复 → 第二笔整笔回滚，无部分领取
test_claimBatch_EmptyAndLengthMismatch             空批次/长度不一致 拒绝
test_claim_LargeIndex_BitmapWord                   索引 300：word1/bit44，位图正确
test_claim_ReentrancySameIndex_Reverts             收款回调中同索引重入 → 回滚
test_claimBatch_ReentrancyOtherIndex_RevertsAll    重入抢占批内另一索引 → 整笔回滚
test_claim_ERC20_Single                            ERC20 模式领取成功
test_claim_ERC20_InvalidItem_RevertsAll            ERC20 批次单项无效 → 回滚
test_claim_ERC20_PaymentFailureRollsBack           ERC20 付款阶段失败 → 整笔回滚
test_leafHash_BindsAllFields                       叶子五字段绑定逐一验证
test_withdraw_OnlyOwner                            仅 owner 可紧急提款
```

Gas 报告（`forge test --gas-report`，关键函数，单次/平均/中位/最大）：

| 函数 | min | avg | median | max |
|---|---|---|---|---|
| `claim` | 24103 | 53023 | 27966 | 84393 |
| `claimBatch` | 23436 | 80921 | 59361 | 173471 |
| `isClaimed`（view） | 2514 | 2514 | 2514 | 2514 |
| `leafHash`（view） | 762 | 762 | 762 | 762 |

## 二、Python 测试（pytest）

命令：`.venv/bin/python -m pytest -v`
（夹具自动：起 Anvil:8546 → `forge build` → CREATE 地址预测部署+注资 20 ETH → 起 uvicorn:8011；
跨链用例另起 Anvil:8547, chainId=5555）

结果：**22 passed, 0 failed, 0 skipped**

- `test_merkle_unit.py`：13 个
  - 叶子编码与独立参照实现 `eth_abi.packed.encode_packed` 逐字节一致；
  - chainId/合约地址/索引/账户/金额任一字段变化叶子即变；
  - 1～8 叶规模下每个叶子的证明都能验通、树深度正确；
  - 篡改证明、换根、错证明 → 验证失败；
  - 有序配对与顺序无关；重复 index/重复 leaf 被拒绝。
- `test_e2e_claim.py`：9 个
  - `/health`、`/status`、后端 root == 链上 root；
  - `/proof/0`、`/verify/0`（本地叶子 == 合约 `leafHash`）；
  - **HTTP 批量领取 [0,1] 成功**；再提交含已领索引 1 的 [1,2] →
    API 400、交易链上回滚、**索引 2 未留下部分领取**；批内重复索引后端直接 400；
  - `/batch/prepare` 只生成 calldata 不上链、不改任何状态；
  - **批次单项无效原子性**：链上直接构造 [4,5,6] 并把第 2 项金额 +1 →
    收据 `status=0`，4/5/6 位图均未置位、合约与三个收款方余额与交易前完全一致；
  - **批内重复索引**：[7,7] → `status=0`；
  - **跨合约重放**：同链部署第二实例 B，用绑定 A 的叶子/证明调 B → `status=0`；
  - **跨链重放**：chainId=5555 的新 Anvil 上，用 chainId=31337 的叶子/证明 → `status=0`；
    换用正确 chainId=5555 的证明后领取成功（证明后端按当前链生成）。

## 三、手动演示（scripts/demo.sh）

`bash scripts/demo.sh`（Anvil:8555，API:8022，避免与机器上其他本地服务端口冲突）

1. 编译 → 起 Anvil → 预测 CREATE 地址并按该地址生成根 → 部署，预存 20 ETH：
   ```json
   {"address": "0x5FbDB2315678afecb367f032d93F642f64180aa3",
    "chain_id": 31337, "allocation_count": 8, "funded_eth": 20.0}
   ```
   （Anvil 上 deployer 首个 CREATE 地址确定性为 `0x5FbD…aa3`，证明地址预测正确。）
2. 正常批量领取 `[0,1,2,3]` → `status=1`，gas_used 约 11.6 万。
3. 重复重放 `[1,4]` →
   ```
   HTTP 400
   {"detail": "链上预检失败（交易将回滚，未发送）：合约 revert: AlreadyClaimed
              (data=0xb3167bfa…0001…)"}
   ```
   提交前的 `eth_call` 预检即识别出 `AlreadyClaimed(1)`，交易不发送、不耗 gas；
   `cast call ... "isClaimed(uint256)(bool)" 4` → **`false`**（索引 4 无部分领取）。

## 四、验收点对照

| 验收要求 | 实现/验证位置 |
|---|---|
| 叶子绑定链 ID、合约地址、索引、账户、数量 | `src/MerkleClaim.sol` `leafHash`；`backend/merkle.py` `encode_leaf`（单测与 `eth_abi` 对照） |
| 跨合约证明重放失败 | Foundry `test_claim_CrossContractReplay_Reverts`；e2e `test_cross_contract_replay` |
| 跨链重放失败（链 ID 绑定） | Foundry `test_claim_CrossChainReplay_Reverts`；e2e `test_cross_chain_replay` |
| 按索引位图防重复 | 映射位图 `_claimedBitmap`；Foundry 跨交易/批内/大索引三测；e2e 两测 |
| 批量操作原子执行 | claimBatch 两阶段（全部校验+置位后才付款）；篡改/重复/付款失败三路径均验证回滚后无残留 |
| 交易失败不留部分已领状态 | Foundry 多项 `RevertsAll` 核位图+核余额；e2e 核 `status=0` 且余额变化量为 0 |

## 五、已知限制 / 未完成项（如实说明）

1. **分配表为内存存储**：`AllocationStore` 是单进程内存态（演示级），未接数据库；
   多副本部署需外接持久化。根的可信来源应是链上 `merkleRoot()`，
   `/allocations` 已返回 `matches_onchain_root` 供核对。
2. **未提供鉴权**：HTTP 接口无认证，仅按题目要求监听本机回环；不要暴露到公网。
3. **未做 Merkle multiproof / 证明压缩**：批量领取每项各带一条 proof，calldata 较大；
   合约里每个叶子独立校验，语义直观、gas 可接受（见报告）。
4. **资金模型为合约预存**：`claim/claimBatch` 非 payable，需部署时或事后由资金方向合约注资；
   owner `withdraw` 可回收未领取资金（未做时间锁）。
5. `forge build` 有若干 lint 警告（循环内 revert、用户可控收款地址）——均为空投合约固有形态，
   收款地址由 Merkle 叶子锁定，不构成额外风险；未逐一加抑制注释。
6. 机器上 8545/8000 端口被其他本地任务占用，因此演示脚本默认改用 **8555/8022**；
   pytest 使用 8546/8011/8547。README 的 8545 示例为标准默认值，如被占用按此调整即可。
