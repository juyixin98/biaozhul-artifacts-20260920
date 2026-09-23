# 匿名资格电路（Anonymous Eligibility zkSNARK）

使用 **Circom + snarkjs + TypeScript** 的纯后端样例：用户在不暴露身份（秘密）和具体年龄的情况下，证明

1. 自己的承诺叶 `leaf = Poseidon(secret, age)` 属于一棵**深度 8** 的 Merkle 树；
2. 年龄满足 **18 ≤ age ≤ 120**（在电路内用约束强制，不是链下判断）；
3. 公开的 `nullifier = Poseidon(secret, eventId)`，由**秘密与活动 ID（域）**共同生成。

公开输入只有三个：`[root, eventId, nullifier]`。
秘密、年龄、Merkle 路径全部是私有输入；证明是**真实 Groth16 zkSNARK**（见证生成 + 可信设置 zkey + 配对验证），没有任何「哈希匹配代替 ZK」的捷径。

```
用户（证明者）                                  活动后端（验证者）
─────────────                                  ─────────────────
secret, age
leaf = Poseidon(secret, age)  ─── 发放阶段 ──▶  写入 Merkle 树，公布 root
nullifier = Poseidon(secret,eventId)
zk 证明：18≤age≤120、leaf 在 root 下、
        nullifier 绑定 secret 与 eventId
                              ─── 参与活动 ──▶  Groth16 验证 + nullifier 去重
                                                 （同活动不可重复，换活动 nullifier 不同）
```

## 目录结构

```
circuits/eligibility.circom   # 电路：年龄范围 + Merkle 包含 + nullifier 绑定
scripts/setup.sh              # 编译电路 + 本地 Groth16 可信设置（仅测试用）
src/hash.ts                   # Poseidon / 随机秘密（circomlibjs，与电路同参数）
src/tree.ts                   # 深度 8 Poseidon Merkle 树（空叶=0，零节点约定）
src/prover.ts                 # 见证（含 R1CS sanity 断言）+ Groth16 prove/verify
src/protocol.ts               # 高层流程：注册、证明、EventVerifier（防重放）
src/demo.ts                   # 端到端演示（含全部负例）
test/eligibility.test.ts      # Vitest 自动化测试
bin/circom                    # circom v2.1.9 linux-amd64 静态二进制
examples/sample-input.json    # 演示运行后生成的一份真实电路输入（含私密值）
build/                        # setup 产物（gitignore，npm run setup 重新生成）
```

## 环境要求

- Node.js ≥ 18（在 18.20 上验证）、npm、bash、约 1–3 分钟 setup 时间
- Linux x86_64（仓库自带 `bin/circom` v2.1.9 静态二进制）；其他平台从
  <https://github.com/iden3/circom/releases> 替换对应二进制即可

## 本地启动

```bash
npm ci            # 按 package-lock.json 锁定版本安装依赖（npm install 亦可）
npm run setup     # 编译电路 + 生成本地测试用 Groth16 proving/verification key
npm run demo      # 端到端演示（6 次真实 Groth16 prove/verify + 4 次见证构造负例）
npm test          # Vitest 自动化测试（27 个用例）
npm run typecheck # TypeScript 严格类型检查
npm run prove     # 用 examples/sample-input.json 真实出证明并验证，写出 sample-proof.json
```

> `npm run setup` 首次执行会生成 2^15 的 powers-of-tau（约 30–60 秒），
> 之后重复运行会复用 `build/ptau/pot15_final.ptau`，只重做 phase-2（秒级）。
> 需要完全重来：`npm run clean && npm run setup`。

setup 完成后电路规模（实测）：

```
# of Constraints: 2463
# of Private Inputs: 18   (secret, age, 8 pathElements, 8 pathIndices)
# of Public Inputs: 3     (root, eventId, nullifier)
Curve: bn128 (BN254), Groth16
```

## 验收命令与预期

| # | 验收点 | 命令 / 位置 | 预期 |
|---|--------|-------------|------|
| 1 | 真实证明生成与验证成功 | `npm run demo` 第 1、4 节 | `✓ Groth16 proof verified` |
| 2 | 年龄范围边界 18/120 | demo 第 4 节；测试 `age accepted` | 18、120、64 通过 |
| 3 | 年龄越界 17/0/121/127/128/超大域元素 | demo 第 5 节；测试 `age rejected` | **见证构造直接抛错**，无证明可生成 |
| 4 | 错误 Merkle 路径（兄弟节点/路径位翻转/非 bit 位） | demo 第 6 节；测试 3 例 | 见证构造抛错 |
| 5 | 篡改公开输入 root/eventId/nullifier | demo 第 7 节；测试 3 例 | Groth16 `verify` 返回 `false` |
| 6 | 同活动重复 nullifier | demo 第 2 节；测试 `replay protection` | 第二次 `already spent` 拒绝 |
| 7 | 换活动可再次使用 | demo 第 3 节；测试 | nullifier 不同，验证通过 |
| 8 | 伪造 nullifier / 年龄与叶不绑定 | demo 第 8 节；测试 2 例 | 见证构造抛错 |

全部通过时 demo 末尾打印 `ALL CHECKS PASSED`，测试全绿：

```bash
npm test
# ✓ happy path ...  ✓ age rejected: below (17) ... ✓ tampered root ... 20+ tests passed
```

## 协议说明

### 电路（`circuits/eligibility.circom`）

```text
leaf      === Poseidon(secret, age)                 # 承诺：年龄与秘密绑定
nullifier === Poseidon(secret, eventId)             # 域分离 nullifier
Num2Bits(7)(age)                                    # age ∈ [0,127]，封死域大值/负数绕过
LessEqThan(8)(18, age) === 1                        # 18 ≤ age
LessEqThan(8)(age, 120) === 1                       # age ≤ 120
逐层 Poseidon(pathElements[i], 按 pathIndices[i] 选左右) === root   # 深度 8 包含证明
```

- **年龄范围**：先用 `Num2Bits(7)` 把 `age` 约束成 7 位无符号整数，再做两次
  `LessEqThan`。否则比较器可被「域大值 / 负编码」满足（这是 circom 比较器常见坑）。
- **Merkle 路径**：每层用二选一多路器按路径位 `pathIndices[i]` 决定当前节点在左还是在右，
  父节点为 `Poseidon(left, right)`，共 8 层；路径位同时约束为 0/1。
- **nullifier 域分离**：`eventId` 是 Poseidon 的第二个输入，同一 `secret` 在不同
  `eventId` 下输出无关；验证者按 `(eventId, nullifier)` 去重即可实现
  「同活动一人一次、跨活动不关联」。

### 见证与证明（`src/prover.ts`）

- `WitnessCalculatorBuilder(wasm, { sanityCheck: true })` + `calculateWTNSBin(input, true)`：
  circom 生成的 WASM 运行时以 `init(1)` 启动，**求解时断言每一条 R1CS 约束**。
  因此任何电路不成立的陈述（越界年龄、错误路径、nullifier 不一致……）在出证明之前就抛异常。
- `snarkjs.groth16.prove(zkey, wtns)` 生成真实 Groth16 证明；
  `snarkjs.groth16.verify(vkey, publicSignals, proof)` 做 BN254 配对验证。
- `build/Verifier.sol` 是 snarkjs 导出的链上验证合约（仅参考，本项目不部署）。

### nullifier 语义

- 同一用户在**同一活动**里每次生成的 nullifier 相同 → 验证者第二次见到即拒绝（重放保护）。
- **不同活动**的 nullifier 是 Poseidon 在不同域下的输出，相互不可链接 → 用户可参加多个活动。
- nullifier 是公开值；**它不揭示 secret**（Poseidon 抗原像），但同一活动内它就是「一次性身份」，
  验证者天然知道「这个 nullifier 对应的人来过」，不知道这个人是谁。

## 可信设置说明（重要）

Groth16 需要每电路一次可信设置，设置中产生的 toxic waste 一旦泄露，持有者可**伪造任意证明**。

本仓库 `scripts/setup.sh` 做的是**单机、确定性熵、仅用于本地测试**的仪式，**严禁用于生产**：

1. **Phase 1（powers of tau，2^15）**：单机 new + 一次 contribute + prepare phase2；
2. **Phase 2（电路相关）**：两次 `zkey contribute`（模拟两个参与方）；
3. 追加一次 **beacon** 贡献（固定 beacon hash、1024 轮），并 `zkey verify` 校验；
4. 导出 `verification_key.json` 与 `Verifier.sol`。

脚本中的 entropy/beacon 均为写死的明文字符串，任何人都能复现 toxic waste —— 这正是测试参数。
生产环境应当：

- 参与或复用经过**多方独立贡献**的 phase-1 仪式（如 Hermez 大型仪式），并核对 contribution hash；
- 针对本电路跑**多方 phase-2 仪式**，只要有一名诚实参与方销毁了随机性，设置即安全；
- 对最终 zkey 做 `zkey verify`，审计并固定 verification key；
- BN254 安全性按当前公开评估约 100-bit 级别，按业务风险评估是否换曲线/证明系统。

## 隐私边界（如实说明）

这套原语提供与不提供的隐私：

**提供**

- 证明不泄露 `secret`、具体 `age`（只暴露「在 18–120 区间内」这一比特）和树中位置；
- 树内成员之间**不可区分**：任何成员都能生成同一公开输入分布下的证明（零知识）；
- 跨活动不可链接：`nullifier` 由 `eventId` 域分离，验证者无法把同一人在两个活动中的行为关联起来。

**不提供 / 已知限制**

- **发放阶段不是匿名的**：把 `leaf = Poseidon(secret, age)` 加入树的一方（签发者）知道
  「哪个真人对应哪个叶」。真实系统需要签发者只在验明年龄后签名/加叶、且之后不可逆向链接，
  或引入多签发者、盲签发等机制。
- **树规模泄露集合大小**：深度 8 = 最多 256 人，空叶用零节点填充，验证者知道成员总数级别的信息。
- **同一活动内可计数**：nullifier 让验证者知道「有多少不同参与者」，这是防重放的必要代价。
- **元数据隐私不归本电路管**：网络地址、提交时间、IP 等需要另行匿名化。
- **前向安全/密钥保管**：`secret` 泄露后，持有旧活动记录的人可把历史 nullifier 与该 secret 关联；
  secret 应由用户端随机生成（`randomSecret()` 用 webcrypto 取 250-bit 随机数）且不出域。
- 本样例用 JS 内存中的 `Set` 做 nullifier 去重；生产需持久化且按活动分表。
- 这是**教学/样例代码**，未经安全审计；电路、依赖版本、仪式流程上线前需专业审计。

## 依赖（锁定版本见 package-lock.json）

| 依赖 | 版本 | 作用 |
|------|------|------|
| circom | 2.1.9（`bin/`） | 电路编译器 |
| circomlib | 2.0.5 | Poseidon / 比较器 / 位分解电路 |
| circomlibjs | 0.1.7 | 链下 Poseidon（与电路常量一致） |
| snarkjs | 0.7.5 | Groth16 setup / prove / verify / Solidity 导出 |
| circom_runtime | 0.1.28 | 见证计算器（开启 sanityCheck） |
| typescript / tsx / vitest | 5.5 / 4.19 / 2.1 | 类型检查、运行、测试 |

## 可复现性

- 电路源码固定；setup 熵在脚本中固定，因此同一仓库生成的 zkey/vkey 字节级可复现；
  证明本身含证明随机性（Groth16 的 blinding），每次 proof 字节不同，但公开信号确定。
- `examples/sample-input.json` 由 demo 生成，包含一份真实可用的全部电路输入
  （**含私密 secret —— 仅本地样例，勿提交真实秘密**）。
