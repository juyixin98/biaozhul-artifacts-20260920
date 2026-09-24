# 哈希时间锁双侧状态（本地双链 HTLC）

在两条**本机 Anvil 测试链**上各部署一份独立的 `HashedTimelock` 合约，用
Solidity + Foundry 编写并测试合约，用 Python + FastAPI + web3.py 提供
**只读**协调接口与端到端场景。所有连接只指向 `127.0.0.1`，只使用 Anvil
自带的公开测试密钥，不接触任何真实网络。

> **范围声明（务必先读）**：本项目不包含、也不声称解决任意跨链原子性。
> 两条链之间**没有任何跨链消息/桥**；每侧合约只强制执行本地那一条腿的
> 哈希锁与时间锁。协调器只读两侧状态并做风险分级，既不持私钥也不发交易。
> 哈希时间锁在“一链长时间不出块且超过两链超时差”时存在已知失效边界
> （见下文「风险边界」），本项目把该边界**演示出来**而不是掩盖它。

---

## 1. 架构

```
        Anvil 链 A (127.0.0.1:8545, chainId 31337)   Anvil 链 B (127.0.0.1:8546, chainId 31338)
        ┌──────────────────────────────┐             ┌──────────────────────────────┐
        │ HashedTimelock (A 腿)         │             │ HashedTimelock (B 腿)         │
        │ Alice 锁 1 ETH -> Bob         │             │ Bob   锁 1 ETH -> Alice       │
        │ T_A = now + TTL_A             │             │ T_B = T_A - Δ (先到期)        │
        └──────────────┬───────────────┘             └──────────────┬───────────────┘
                       │ eth_call / view（HTTP，只读）              │
                       └───────────────┬───────────────────────────┘
                                       │
                          ┌────────────┴────────────┐
                          │ FastAPI 协调器（:8077）   │  无私钥、无写操作
                          │ /health /convention      │  /swaps/{chain}/{id} /pair
                          └─────────────────────────┘
        锁仓/领取/退款等写操作：scripts/*.py 与 tests/* 用 web3.py 本地签名后发送
```

- 合约：`src/HashedTimelock.sol`（Solidity 0.8.24，Foundry 1.8.3 编译/测试）
- Python：`htlclib/`
  - `chains.py`：Anvil 进程管理、健康探测、时间戳/挖矿控制（模拟停链）
  - `contract.py`：构件加载、部署、签名客户端、swapId 派生、状态视图
  - `coordinator.py`：FastAPI 只读协调器
  - `harness.py`：演示与测试共用的三个场景
- 脚本：`scripts/deploy.py`（部署）、`scripts/demo.py`（端到端演示）、
  `scripts/run-tests.sh`（一键全量测试）
- 测试：`test/`（Foundry，14 个）、`tests/`（pytest，26 个）

## 2. 时间条件与两链超时差约定

- 哈希：`hashLock = keccak256(preimage)`，原像 32 字节。
- 时间：EVM `block.timestamp`，Unix 秒；`refund` 在 `block.timestamp >= timelock`
  时可用（即**恰好等于截止时刻可退款**，由测试固定）。
- 两条腿的截止时刻：

  | 腿 | 方向 | 截止时刻 |
  |---|---|---|
  | A | Alice → Bob | `T_A = now + TTL_A`（默认 `TTL_A = 60s`，可用 `HTLC_TTL_A_SECONDS` 覆盖） |
  | B | Bob → Alice | `T_B = T_A − Δ`（默认 `Δ = 20s`，可用 `HTLC_DELTA_SECONDS` 覆盖） |

  即 **B 腿比 A 腿早 Δ 秒到期**。含义：领取方（在 B 上揭示原像的 Alice）
  在 B 到期之后仍有 Δ 秒在 A 上完成领取。**Δ 必须大于任一链最坏情况下的
  出块/暂停延迟**；真实部署应按目标链确认时间保守取值。

## 3. 状态机与互斥

```
NONEXISTENT ──lock──► LOCKED ──claim(正确原像)──► CLAIMED   （终态）
                           │
                           └────refund(本人 && now≥T)──► REFUNDED（终态）
```

- `claim`：状态为 `LOCKED` 且 `keccak256(preimage) == hashLock` 即可领取；
  **在截止时刻之后只要仍为 LOCKED，揭示原像依然胜出**（这是标准 HTLC 语义，
  也是 Δ 存在的原因）。
- `refund`：仅原锁定人、且 `now ≥ T`、且仍为 `LOCKED`。
- `CLAIMED`/`REFUNDED` 互为终态：重复领取、重复退款、领取后退款、退款后领取
  全部以自定义错误 `AlreadySettled` 回滚（见验收测试）。
- `swapId = keccak256(abi.encodePacked(hashLock, sender, receiver, amount, timelock))`，
  权威值以 `Locked` 事件为准；Python 侧 `derive_swap_id` 按相同 packed 编码交叉验证。

## 4. 风险边界（本项目展示什么、不展示什么）

1. **安全中止（停链时长 < Δ，且未揭示原像）**：A 链暂停 3 秒（关闭出块），
   期间 RPC 仍响应但区块号冻结；恢复后双方都不揭示原像，各自等截止时刻后
   退款，**无人损失**。
2. **超时差失效（原像已揭示 + 停链时长 > Δ）**：Alice 在 B 到期前 1 秒领取
   （原像公开），A 链随即长时间不出块；恢复时已过 `T_A`，Alice 的退款先落账，
   Bob 随后的领取被 `AlreadySettled` 拒绝 → **B=CLAIMED / A=REFUNDED，Bob
   承担 1 ETH 损失**。真实链上到期点的打包顺序是费率/中继竞争，合约无法保证
   超时后“领取优先于退款”；没有任何规则能在 Δ 被超过后让双方同时赢。
3. **一链彻底宕机**：杀掉一个 Anvil，协调器 `/health` 返回 503，对应单腿查询
   返回 502，`/pair` 标记 `unreachable`——协调器能如实报告“无法判断联合状态”，
   但不能代用户完成或回滚任何一笔交易。

资金不会卡在合约里：任何结局都是 CLAIMED 或 REFUNDED；但**损失归属**可能不对等，
这正是 HTLC 的已知边界，而非本项目的缺陷或被解决的问题。

## 5. 依赖

- 系统：Linux/macOS，`git`、`curl`、Python ≥ 3.10（实测 3.12.3）
- Foundry（`forge`/`anvil`/`cast`，实测 1.8.3）。若未安装：
  ```bash
  curl -L https://foundry.paradigm.xyz | bash && ~/.foundry/bin/foundryup
  ```
  forge-std 已随 `lib/forge-std` 提交；solc 由 `forge build` 按需下载。
- Python 依赖（已锁定，见 `requirements-lock.txt`，57 个包含传递依赖）：
  fastapi 0.115.6、uvicorn 0.34.0、web3 7.6.1、pytest 8.3.4、httpx 0.28.1。
  ```bash
  python3 -m venv .venv && source .venv/bin/activate
  pip install -r requirements.txt          # 或 pip install -r requirements-lock.txt
  ```

## 6. 启动与使用命令

```bash
# 0) 编译合约（生成 out/ 供 Python 加载 ABI/字节码）
forge build

# 1) 一键全量测试（Foundry + pytest，会自动拉起/销毁临时 Anvil）
bash scripts/run-tests.sh
#    或分开运行：
forge test -vv
python -m pytest tests/

# 2) 端到端演示（自带两条临时 Anvil，跑完即销毁；写 demo-results.json）
python scripts/demo.py

# 3) 手动方式：自己起两条链
anvil --port 8545 --chain-id 31337 --silent
anvil --port 8546 --chain-id 31338 --silent
#    部署（若链已在运行）：
python scripts/deploy.py
#    或让脚本负责起链（Ctrl-C 时一起停掉）：
python scripts/deploy.py --spawn

# 4) 启动只读协调器（8000 被占用时换端口，如 8077）
python -m uvicorn htlclib.coordinator:app --host 127.0.0.1 --port 8077
curl -s localhost:8077/health | jq .
curl -s localhost:8077/convention | jq .
curl -s "localhost:8077/pair?swap_id_a=<0x..>&swap_id_b=<0x..>" | jq .
#    /pair 也支持用锁仓参数派生两侧 id：hash_lock、sender_a、receiver_a、
#    sender_b、receiver_b、amount_wei、timelock_a（T_B 按 Δ 自动减）。
```

## 7. 实际运行结果（如实记录）

运行环境：Linux 6.8、Python 3.12.3、Foundry 1.8.3、web3.py 7.6.1，全部针对
`127.0.0.1` 上的临时 Anvil。完整输出归档于 `docs/test-output.txt`，
演示结果归档于 `docs/example-demo-results.json`。

- `forge test`：**14 passed, 0 failed**（锁仓、正确/错误原像、截止时刻前后、
  非锁定人退款、重复领取/退款、领取↔退款互斥、过去时间锁、零金额）。
- `pytest tests/`：**26 passed, 0 failed**：
  - 合约行为（11）：正常双侧领取、swapId 派生一致、错误原像回滚且保持
    LOCKED、`T-1` 拒绝退款 / `T` 恰好可退、非本人退款拒绝、双领取/双退款
    拒绝、退款后领取与领取后退款均拒绝、截止后 LOCKED 仍可被原像领取、
    过去时刻锁仓拒绝、未知 swap 为 NONEXISTENT；
  - 风险边界（4）：停链 3s<Δ 安全中止（区块冻结/恢复均有断言）、揭示后
    停链 >Δ 出现 B=CLAIMED/A=REFUNDED 且延迟领取被 `AlreadySettled` 拒绝、
    暂停期间待确认交易无法上链、Δ 约定与同一 hashLock 两侧一致；
  - 协调 API（11）：双侧在线健康检查、一链宕机 503、约定端点、单腿读取、
    单链宕机时 502、`/pair` 的 settled/in_progress/unreachable 分级、
    由锁仓参数派生 id、参数缺失 400。
- `python scripts/demo.py`：退出码 0；三个场景分别得到
  `{A: CLAIMED, B: CLAIMED}`、`{A: REFUNDED, B: REFUNDED}`（halt=3s<Δ=20s）、
  `{A: REFUNDED, B: CLAIMED}`（Bob 延迟领取回滚 `AlreadySettled`，Bob 损失 1 ETH）。
- 另做过真实 HTTP 链路手测：`deploy.py --spawn` 部署后用 uvicorn 起服务，
  链上锁仓 2 ETH → `GET /pair` 返回 `in_progress`，另一对锁仓双侧领取后
  返回 `settled`（该手测用临时链，进程已销毁，未持久化）。

## 8. 明确的限制与未完成项

- 不解决、也不尝试解决任意跨链原子性；没有中继者网络、没有消息协议、
  没有超时后“领取优先”的打包顺序保证。
- 协调器**只读**；锁仓/领取/退款的自动化写操作只存在于脚本和测试中，
  未提供带鉴权的写 API（按题意 HTTP 仅作只读协调）。
- Δ、TTL 是教学默认值（60s/20s），不对应任何真实公链的确认预算；
  生产前必须按目标链重估。
- 停链用 Anvil 的 `evm_setAutomine(false)` 与时间戳 warp 模拟，能重现
  “无块、时间推移、恢复”的关键性质，但不模拟真实分叉、交易池驱逐、
  节点分区等行为。
- 合约只处理原生 ETH；ERC20 变体未实现。未做形式化验证/Gas 优化/审计，
  请勿用于真实资产。

## 9. 目录

```
src/HashedTimelock.sol          HTLC 合约
test/HashedTimelock.t.sol       Foundry 单元测试（14）
htlclib/chains.py               Anvil 管理 / 时间与挖矿控制
htlclib/contract.py             部署 + 签名客户端 + 视图
htlclib/coordinator.py          FastAPI 只读协调器
htlclib/harness.py              三个可复用场景
scripts/deploy.py               部署到两条链
scripts/demo.py                 端到端演示
scripts/run-tests.sh            一键全量测试
tests/*.py                      pytest（26）
docs/example-demo-results.json  归档的演示结果
docs/test-output.txt            归档的全量测试输出
requirements.txt / -lock.txt    直接依赖 / 锁定版本
```
