# 哈希时间锁双侧状态（Two-Sided HTLC）

在**两条本机 Anvil 测试链**上部署同一份 HTLC（Hashed Timelock Contract）合约，
配一个 **FastAPI 只读协调接口**（web3.py），呈现同一笔跨链交换在两侧的
锁定 / 领取 / 退款状态、时间锁约定与**风险边界**。

> ⚠️ 范围声明：本项目演示的是 **HTLC 原语 + 双侧只读观察**。
> HTLC **不保证任意跨链原子性**。每一侧合约只能强制执行本链上的规则；
> 两侧之间没有任何事务或共识联系。时间锁到期、链停机、对手方不作为等
> 都可能导致"一侧 Claimed、另一侧 Refunded"的非原子结局。
> 协调器**只读取状态并给出建议，不保管私钥、不发送交易**，无法替任何一方
> 消除这种风险（见文末《风险边界与未解决问题》）。

---

## 1. 架构

```
                ┌─────────────────────────────┐
                │  FastAPI 只读协调器 (Python) │  GET /health /swap/{id} /swaps
                │  app/  (web3.py, HTTP RPC)  │  不持有私钥，无任何写接口
                └──────────────┬──────────────┘
                  JSON-RPC     │      JSON-RPC
              ┌────────────────┴────────────────┐
              ▼                                  ▼
   Anvil「Alpha」chainId 31337        Anvil「Beta」chainId 31338
   127.0.0.1:8645                    127.0.0.1:8646
        HTLC.sol                            HTLC.sol
   （同一份字节码，各自独立部署、各自独立状态）
```

- `contracts/HTLC.sol` — HTLC 合约，Solidity `0.8.26`，无第三方 Solidity 依赖。
- `test/HTLC.t.sol` — 合约层 Foundry 测试（15 个）。
- `app/` — Python：`chain.py`（单链读写、自定义错误解码）、`coordinator.py`
  （双侧状态评估与建议）、`main.py`（FastAPI）、`config.py`。
- `tests/` — 端到端 pytest（30 个）：自动起两条 anvil、实际发交易、
  精确控制区块时间戳、SIGSTOP 模拟停机。
- `scripts/` — `anvil-start.sh` / `anvil-stop.sh` / `deploy.py` / `demo.py` / `test-all.sh`。

### 单侧状态机（两条链上各自独立运行）

```
Absent ──lock()──▶ Locked ──claim(preimage)──▶ Claimed   （终态）
                     │
                     └──────refund() 到期────▶ Refunded  （终态）
```

`claim` 与 `refund` 互斥：两者都只在 `Locked` 时成立，并严格按
**checks-effects-interactions** 先置终态再转币。任何一方成功后，
另一方在任何时刻（无论是否到/过时间锁）都必然以自定义错误回滚。

| 操作 | 条件 | 失败回滚（自定义错误） |
|---|---|---|
| `lock(id,receiver,hashLock,timelock)` 带原生币 | swapId 不存在、金额>0、receiver≠0 | `SwapExists` / `ZeroAmount` / `ZeroReceiver` |
| `claim(id,preimage)` | Locked、调用者=receiver、`keccak256(preimage)==hashLock` | `SwapNotFound` / `AlreadySettled` / `NotReceiver` / `WrongPreimage` |
| `refund(id)` | Locked、`block.timestamp >= timelock` | `SwapNotFound` / `AlreadySettled` / `TooEarly(now,timelock)` |

哈希锁统一为 `keccak256(abi.encodePacked(bytes32 preimage))`，原像为 32 字节。

---

## 2. 时间条件与两链超时差（明确约定）

设交换双方为 Alice（Alpha 链上的付款人）与 Bob（Beta 链上的付款人），
同一 `swapId`、同一 `hashLock` 在两侧各锁一笔。

```
                 tB（Beta 时间锁）                    tA（Alpha 时间锁）
  锁定 ──────────│────────────── Δ = tA − tB ─────────│──────────── 时间
                 ▼                                    ▼
          Beta 到期可退款                      Alpha 到期可退款
```

**约定：`tB < tA`（Beta 先到期，Alpha 后到期），且 `Δ = tA − tB` 必须足够大。**

为什么是这个顺序：第一笔 `claim` 会在**该侧链上公开 preimage**。
让"先到期的一侧"（Beta）成为领取通常发生的一侧——受益人一旦在 Beta 领取，
preimage 即公开；另一受益人需要 `Δ` 这段时间在 Alpha 完成领取。

- 若 Beta 无人领取：到 `tB` 后 Bob 退款；随后 Alice 也可在 `tA` 后退款。双方安全退出。
- 若 Beta 被领取：preimage 公开，Alpha 受益人必须在 **Alpha 截止 `tA` 之前**
  用同一 preimage 领取。`Δ` 必须覆盖"看到 Beta 事件 → 在 Alpha 提交并上链"
  的**最坏**耗时（出块、网络、RPC 延迟、自己的反应时间）。
- 协调器对每次评估返回 `time_plan`：
  - `deadline_order_ok = (tB < tA)`；
  - `buffer_ok = (Δ ≥ min_delta)`，`min_delta` 默认 **15 秒**
    （`coordinator.DEFAULT_MIN_DELTA_SECONDS`，仅本地演示阈值，可改）。
  顺序错误或裕度不足都会产生显式 `risk_flags`。

两条链各用自己的 `block.timestamp`，**互不同步**；Anvil 默认随块实时推进，
测试里用 `evm_setNextBlockTimestamp` 精确钉死边界。

---

## 3. 环境与依赖

- Linux/macOS，[Foundry](https://book.getfoundry.sh/)（`forge`/`anvil`/`cast`，本仓库在 **1.8.3** 上验证）。
  本项目 Solidity **零第三方依赖**（不用 forge-std，测试内置最小 `Vm` 接口）。
- Python **3.12**（3.10+ 亦可），依赖见 `requirements.txt`，
  **完整锁定版本**见 `requirements.lock`（62 个包，含传递依赖）。
  核心：`fastapi==0.115.6`、`uvicorn==0.34.0`、`web3==6.20.3`、
  `eth-abi==4.2.1`、`eth-account==0.11.3`、`httpx==0.28.1`、`pytest==8.3.4`。

> 仅使用 Anvil/Hardhat 的**公开确定性测试私钥**，只能打本机测试链，
> 切勿在任何真实网络使用。

安装（在仓库根目录）：

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock     # 精确锁定版本
forge build                                    # 编译合约到 out/
```

---

## 4. 启动命令（手动演示）

```bash
# 1) 起两条链（默认 8645/8646；状态落盘 .run/，停止后可恢复）
bash scripts/anvil-start.sh

# 2) 两侧部署 HTLC，写 deployment.json
.venv/bin/python scripts/deploy.py

# 3) 只读协调接口（本环境 8000 常被占用，这里用 8700）
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8700

# 4) 四个端到端场景（另开一个终端）
.venv/bin/python scripts/demo.py happy      # 双侧领取
.venv/bin/python scripts/demo.py timeout    # 截止前拒退 → 到点退款
.venv/bin/python scripts/demo.py badclaim   # 错误原像/重复领取/终态后退款
.venv/bin/python scripts/demo.py pause      # SIGSTOP 暂停 Alpha，展示风险边界
.venv/bin/python scripts/demo.py all        # 全部

# 5) 收尾停链（SIGTERM，状态写回 .run/*-state.json）
bash scripts/anvil-stop.sh
```

端口/链 ID 可用环境变量覆盖：
`ALPHA_PORT`、`BETA_PORT`、`HTLC_ALPHA_RPC`、`HTLC_BETA_RPC`、
`HTLC_ALPHA_CHAIN_ID`、`HTLC_BETA_CHAIN_ID`、`HTLC_RPC_TIMEOUT`。

> 说明：默认端口刻意不用 anvil 默认的 8545/8546（本机可能已有其它 anvil）。
> `anvil-start.sh` 会检测端口是否被**别的进程**占用并直接报错，避免误连到他人的链。

### HTTP 接口（全部为只读 GET）

| 路径 | 说明 |
|---|---|
| `GET /health` | 两侧 RPC 连通性、chainId、合约地址、当前块高 |
| `GET /config` | 当前使用的两侧 RPC/链 ID/合约地址 |
| `GET /swap/{32字节hex}` | 双侧状态 + `time_plan` + `status` + `risk_flags` + `advice`；Beta 已领取时回带公开的 `preimage` |
| `GET /swaps` | 两侧历史上所有 Locked 的 swapId 汇总 |

`status` 取值（部分）：`LOCKED_BOTH`、`ONE_SIDED_LOCK`、`HASH_MISMATCH`、
`BETA_CLAIMED_ALPHA_PENDING`、`CLAIMED_BOTH`、`REFUNDED_BOTH`、
`BETA_REFUNDED_ALPHA_LOCKED`、`SPLIT_BETA_CLAIMED_ALPHA_REFUNDED`（非原子结局）、
`CHAIN_UNREACHABLE`。交互式文档：`http://127.0.0.1:8700/docs`。

---

## 5. 自动化测试

一键：

```bash
bash scripts/test-all.sh
```

- `forge test`（15 个，纯 EVM，毫秒级）：锁定与事件、正确/错误原像、
  非受益人、**截止时刻边界 `t-1` 拒绝 / `t` 精确可退**、重复领取、
  重复退款、claim→refund 与 refund→claim 互斥、同 id 重锁、未知 swap、
  领取时重入不能二次结算。
- `pytest tests/`（30 个）：夹具自动起两条**测试专用** anvil
  （18645/18646）、部署合约、真实签名发交易，结束自动清理。
  - `test_htlc_e2e.py`：双侧领取、错误原像不改变状态、重复领取/退款、
    截止时刻、互斥、资金真实转移；
  - `test_coordinator_e2e.py`：`tB<tA` 校验、`Δ` 过小、单侧锁定、
    hashLock 不一致、Beta 已领取公开 preimage、双侧退款、
    **刻意构造的非原子 split 结局**、**SIGSTOP 一链暂停→不可达→恢复**；
  - `test_api_e2e.py`：FastAPI 全端点、400 校验、停机时接口 2 秒内如实返回。

---

## 6. 实际运行结果（如实记录）

以下在本仓库实际执行（Linux 6.8，Foundry 1.8.3，Python 3.12.3，日期 2026-09-24）。

**合约测试**

```
forge test → 15 passed, 0 failed, 0 skipped
```

**端到端测试**

```
pytest tests/ → 30 passed, 1 warning in ~15s
```

**手动四场景**（完整输出见 `docs/demo-output.txt`）：

1. `happy`：双侧锁定（`tB<tA, Δ=30s`）→ Beta 领取（preimage 公开）
   → Alpha 用同一 preimage 领取 → `CLAIMED_BOTH`。
2. `timeout`：`tB` 前退款被拒（`TooEarly(nowTs=…, timelock=…)`），
   到点 Beta `Refunded`，协调器提示"Alpha 此时领取等于白送"，再到 `tA` 双侧 `Refunded`。
3. `badclaim`：错误原像 `WrongPreimage`、非受益人 `NotReceiver`、
   重复领取与领取后退款均 `AlreadySettled(state=2)`，全部回滚且状态不变。
4. `pause`：对 Alpha anvil 发 `SIGSTOP`（Beta 继续），
   协调器 **2 秒内**返回 `CHAIN_UNREACHABLE` 并给出停机期间风险；
   `SIGCONT` 后锁定状态原样保留，随后完成双侧领取。

另通过 pytest 与 HTTP 实际演示了 **`SPLIT_BETA_CLAIMED_ALPHA_REFUNDED`**
非原子结局：构造 `tA` 很近、先在 Alpha 超时退款、再在 Beta 领取，
协调器明确标记"非原子结果已经发生，HTLC 原语无法回滚"。

实现过程中遇到并修复的真实问题（均已在最终代码中解决）：
`eth-abi==4.2.3` 在 PyPI 不存在（锁定为存在的 4.2.1）；
本机 8545/8000 端口已被**其它进程**占用导致误连/绑定失败
（改用 8645/8646/8700 并在启动脚本里做端口归属校验）；
`evm_setNextBlockTimestamp` 后误先 `evm_mine` 导致边界测试时间错位
（改为把时间戳钉在交易所在区块）；web3 v6 的 `rawTransaction` 命名、
默认中间件覆盖 AttributeDict、provider 层 `http_retry_request`
把停机判定拖到 ~11s（注入无重试 session 并移除该中间件后为 ~2s）。

---

## 7. 风险边界与未解决问题（不夸大为"跨链原子"）

1. **没有跨链原子性保证。** 两个 leg 是独立链上的独立合约。
   唯一的耦合是同一个 preimage；当"消息/行动时间 > 剩余时间窗"时，
   仍会出现一侧领取、一侧退款。`Δ` 只是概率上的工程裕度，不是共识保证。
2. **时间锁依赖各链 `block.timestamp`。** 出块调度、时间戳漂移、链停机/重组
   都可能让真实窗口与预期不同；本项目不处理跨链时间预言机。
3. **一链暂停/宕机期间协调器无能为力。** 它只读、不持私钥、不发交易。
   `CHAIN_UNREACHABLE` 只是把"现在无法判断/无法行动"显式告诉你。
   SIGSTOP 模型里对端 TCP 接受连接但不响应，已把只读超时压到约 2 秒。
4. **退款与领取的链上竞争（同一 leg 内）在到达时间锁后依然存在。**
   合约规则是"谁的交易先在 Locked 状态下被打包谁生效"，
   之后另一方必然 `AlreadySettled`。这是 HTLC 的标准语义，不是 bug，
   但它意味着"刚过截止才提交领取"可能输掉竞争。
5. **不包含**：代币（ERC20）锁定、swapId 抢注防护、手续费模型、
   持久化订单簿、鉴权、重放保护的额外封装、事件重组处理、生产级监控。
   原像固定为 32 字节、单一 receiver，演示用，未做审计。
6. 协调器的 `min_delta=15s` 是**本地演示阈值**，不代表任何真实网络的安全值。
