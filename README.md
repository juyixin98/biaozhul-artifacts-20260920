# 匿名资格电路 (Anonymous Eligibility Circuit)

基于 **Circom + snarkjs (Groth16) + TypeScript** 的本地匿名资格证明样例，纯后端，无前端。

证明人在**不泄露年龄、秘密与身份**的前提下证明：

1. 年龄在 **18 ~ 120** 之间（含边界）；
2. 其私密叶 `Poseidon(age, secret)` 属于一棵**深度 8** 的 Poseidon Merkle 树（成员名单承诺）；
3. 公开 **nullifier** 由 `Poseidon(secret, activityId)` 正确生成 —— 同一秘密在同一活动下 nullifier 唯一（**同活动不可重复**），换活动（换 `activityId`）产生全新 nullifier（**换活动可使用**），且各活动之间不可链接。

公开输入（3 个）：`root`（Merkle 根）、`activityId`（活动域）、`nullifier`（作废符）。
私密输入：`age`、`secret`、`pathElements[8]`、`pathIndices[8]`。

所有证明均为**真实生成与验证**（witness 计算 + Groth16 证明 + 验证密钥校验），没有任何哈希匹配代替 ZK 的捷径。

## 目录结构

```
circuits/eligibility.circom   # 主电路: 年龄范围 + Merkle 成员 + nullifier 绑定
src/poseidon.ts               # Poseidon 哈希 (circomlibjs, 与电路同参数)
src/merkle.ts                 # 深度 8 Poseidon Merkle 树与包含证明
src/eligibility.ts            # 叶/nullifier 计算与电路输入组装
src/prover.ts                 # Groth16 证明生成 (snarkjs fullProve)
src/verifier.ts               # 密码学验证 + nullifier 注册表 (防重放)
src/cli.ts                    # 命令行: prove / verify / demo
scripts/setup.sh              # 编译电路 + 本地可信设置 (powers of tau + zkey)
inputs/example.json           # 示例输入 (成员名单 + 证明人索引 + 活动 ID)
test/eligibility.test.ts      # 自动化测试 (node:test)
```

## 本地启动

要求：Node.js ≥ 18，npm。

```bash
npm install        # 安装锁定依赖 (package-lock.json)
npm run setup      # 编译电路 + 本地可信设置 (约 1~2 分钟, 幂等, 已有产物则跳过)
```

## 验收命令

```bash
npm test                             # 全部自动化测试 (11 个用例)
npm run prove -- inputs/example.json # 用示例输入真实生成证明 -> build/proof.json
npm run verify                       # 验证证明并登记 nullifier (再次执行会因重复 nullifier 被拒绝)
npm run demo                         # 端到端演示: 合法证明 / 篡改公开输入 / 换活动
npm run typecheck                    # TypeScript 类型检查
```

`npm run verify` 第二次运行会输出 `nullifier already used: 同一活动不可重复提交` 并以非零码退出 —— 这是防重放机制按预期工作。删除 `build/nullifiers.json` 可重置本地注册表。

## 测试覆盖

| 用例 | 预期 |
|---|---|
| 合法证明 | 真实生成并通过 `groth16.verify` |
| 年龄边界 18 / 120 | 均可证明 |
| 年龄越界 17 / 121 / 0 / 200 | 见证生成阶段失败（电路断言） |
| 篡改 Merkle 路径元素 | 见证生成失败 |
| 非成员秘密 | 见证生成失败 |
| 篡改公开输入 root / activityId / nullifier | 验证返回 false |
| 篡改证明本体 `pi_a` | 验证返回 false |
| nullifier 正确性 | 等于 `Poseidon(secret, activityId)` 且出现在公开输入中 |
| 换活动 | 同一秘密产生不同 nullifier，均可验证 |
| 同活动重复 nullifier | 首次接受，重放与重新生成的证明均被拒绝 |
| 根/活动与预期不符 | 应用层拒绝 |

## 协议与参数

- **曲线**：BN128（bn254），Groth16 证明系统。
- **哈希**：Poseidon（circomlib 参数），电路内与电路外（circomlibjs）使用同一实现，保证叶、根、nullifier 一致。
- **电路规模**：约 5.2k 约束（10 个 Poseidon 双输入哈希 + 两个 7 比特范围检查）。
- **年龄范围证明**：`age - 18 ∈ [0,127]` 且 `120 - age ∈ [0,127]`（各用一个 `Num2Bits(7)`），两式同时成立当且仅当 `18 ≤ age ≤ 120`，且约束年龄为整数。
- **nullifier 构造**：`nullifier = Poseidon(secret, activityId)`。`activityId` 即活动域分隔符：域不同则 nullifier 无关；域相同则同一 `secret` 必得同一 nullifier，验证方据此去重。

## 可信设置说明

`npm run setup` 在本地执行**单人** powers of tau（2^13）与电路专用 zkey 贡献，熵来自 `/dev/urandom`，另加一次固定 beacon。**这仅适用于本地开发与测试**：Groth16 的安全性要求设置仪式中至少一方诚实并销毁自己的秘密份额（"toxic waste"）。单人设置意味着该方掌握完整 toxic waste，可以伪造任意证明。

生产环境必须：

1. 使用公开的多方计算（MPC）仪式产物（如 Hermez/Perpetual Powers of Tau，或自行组织多方贡献）；
2. 电路专用阶段（phase 2）同样采用 MPC，并公开贡献者名单与贡献哈希以供验证；
3. 用 `snarkjs zkey verify` 校验最终 zkey 确实由仪式产物导出。

## 隐私边界

**证明隐藏的内容**：年龄具体值（仅知落在 [18,120]）、长期秘密 `secret`、证明人在 Merkle 树中的位置（叶索引与路径）、树中其他成员信息。

**证明公开/可推断的内容**：

- `root`、`activityId`、`nullifier` 是公开输入，验证方及任何观察者可见；
- 同一活动内，同一用户的多次提交产生**相同 nullifier** —— 这正是防重放依据，但也意味着该活动内可观察到"同一人重复尝试"；
- 跨活动不可链接（nullifier 不同且无有效关联方法），前提是 `secret` 不泄露且各活动 ID 不同；
- Merkle 树本身（成员名单）若在别处公开，结合链外信息可能缩小匿名集；匿名性上限为树中成员数（本电路容量 256）；
- 若 `secret` 泄露，攻击者可冒充该成员生成证明，因此 `secret` 必须本地妥善保管；
- 本样例不隐藏证明提交的**时间与网络元数据**（IP 等），生产部署需配合匿名网络。

## 测试参数声明

`inputs/example.json` 中的年龄与秘密、`scripts/setup.sh` 中的 beacon 值均为**本地测试参数**，不对应任何真实身份，不得用于生产。
