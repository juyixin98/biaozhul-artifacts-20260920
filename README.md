# 线性释放代币托管（Linear Token Vesting Escrow）

使用 **Solidity + Foundry** 实现的链上悬崖期/线性释放代币托管合约，配合
**Python + FastAPI + web3.py** 的本地 HTTP 查询与操作接口。链环境仅使用本机
**Anvil** 与 Anvil 内置测试密钥，不连接任何公链。

## 功能

- **合成代币**：`SyntheticToken`（SYN，18 decimals），仅用于本地测试。
- **悬崖期（cliff）**：悬崖到达前归属量为 0；悬崖时刻按线性曲线一次性归属对应比例。
- **线性释放**：`vested = total * (t - start) / (end - start)`，先乘后除，全程
  Solidity 0.8 checked arithmetic，溢出直接 revert，不存在回绕。
- **撤销（revoke）**：归属在撤销时刻冻结；**已归属部分仍归受益人**（可继续领取），
  未归属部分立即退回 owner。支持「先撤销后领取」与「领取—撤销—再领取」两种交错顺序。
  注意：撤销瞬间守恒式为 `已领 + 已退 + 托管中待领(=当时已归属未领) == 锁定量`；
  受益人把剩余已归属部分领走后，`released + refunded == totalLocked` 恰好成立。
- **非整除总额**：整除余数（最多 1 个最小单位）在 `end` 时刻自动归到最后一笔领取，
  合约不会永久卡住 dust。
- **守恒不变量**：生命周期结束后
  `releasedToBeneficiary + refundedToOwner == totalLocked`，托管合约余额为 0。

## 目录结构

```
src/                    Solidity 合约
  SyntheticToken.sol    合成 ERC-20
  LinearTokenVesting.sol 悬崖 + 线性释放 + 可撤销托管
test/                   Foundry 合约测试（15 个）
scripts/deploy.sh       构建 + Anvil 部署脚本
api/                    FastAPI 服务 + web3.py 客户端
  main.py               HTTP 路由
  client.py             链上读写封装（发交易前先 eth_call 模拟以暴露 revert）
  config.py             RPC/测试密钥/地址配置
tests/                  Python 集成测试（真实 Anvil + TestClient，9 个）
examples/demo.py        端到端示例脚本
deploy/addresses.json   部署产物（脚本生成）
foundry.toml
requirements.txt        锁定的 Python 依赖版本
```

## 依赖

| 组件 | 版本 |
|---|---|
| Foundry（forge/anvil/cast） | 1.8.3（任何较新 0.8.x 工具链均可，`foundryup` 安装） |
| Solidity | 0.8.24（foundry.toml 固定） |
| Python | 3.12 |
| Python 包 | 见 `requirements.txt`：fastapi 0.115.5、uvicorn 0.32.1、web3 7.5.0、pydantic 2.10.3、pytest 8.3.4、httpx 0.28.1 |
| forge-std | 由 `forge install foundry-rs/forge-std` 安装到 `lib/` |

## 安装

```bash
# 1) Foundry（若尚未安装）
curl -L https://foundry.paradigm.xyz | bash
foundryup
export PATH="$HOME/.foundry/bin:$PATH"
forge install foundry-rs/forge-std   # 本仓库已安装到 lib/

# 2) Python 依赖
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 启动

```bash
# 终端 1：启动本地 Anvil（默认测试密钥）
anvil --host 127.0.0.1 --port 8545 --chain-id 31337

# 终端 2：构建并部署合约（生成 deploy/addresses.json）
source .venv/bin/activate
./scripts/deploy.sh

# 终端 3：启动 HTTP 服务
uvicorn api.main:app --host 127.0.0.1 --port 8000
# 文档： http://127.0.0.1:8000/docs
```

也可以让部署脚本自动拉起 Anvil：`START_ANVIL=1 ./scripts/deploy.sh`。

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 链状态、代币信息、托管合约总余额 |
| GET | `/schedules/{id}` | 查询计划：总额/已领/已退/起止/悬崖/当前归属与可领 |
| POST | `/schedules` | 创建计划（body 指定受益人、金额、时间参数） |
| POST | `/schedules/{id}/release` | 领取当前已归属部分（默认用 Anvil 账户 #1 签名） |
| POST | `/schedules/{id}/revoke` | owner 撤销（仅 revocable 计划），未归属部分立即退回 |
| POST | `/test/evm/fast-forward` | Anvil 测试辅助：推进链时间并出块 |
| GET | `/tokens/{address}` | 查询 SYN 余额 |

### curl 示例

```bash
# 创建：1000 SYN，start=现在，cliff=60s，线性 600s，可撤销
curl -s -X POST localhost:8000/schedules -H 'Content-Type: application/json' -d '{
  "beneficiary": "0x70997970C51812dc3A010C7d01b50e0d17dc79C8",
  "amount": 1000000000000000000000,
  "start_timestamp": '$(date +%s)',
  "cliff_duration": 60,
  "vesting_duration": 600,
  "revocable": true
}'

curl -s localhost:8000/schedules/0
curl -s -X POST localhost:8000/test/evm/fast-forward -H 'Content-Type: application/json' -d '{"seconds":300}'
curl -s -X POST localhost:8000/schedules/0/release
curl -s -X POST localhost:8000/schedules/0/revoke
```

## 测试

```bash
# 合约单元测试（Foundry，无需手动起 Anvil）
forge test -vv

# Python 端到端测试（自动在 8547 端口启动独立 Anvil、部署合约、跑 HTTP API）
source .venv/bin/activate
python -m pytest tests/ -v
```

合约测试覆盖：起/止/悬崖边界、同块与跨时间重复领取、两种撤销/领取交错顺序、
悬崖内撤销、期满后撤销、非整除总额 dust、uint128 级大额防溢出、非法参数拒绝。
集成测试通过 web3.py + FastAPI TestClient 对真实 Anvil 复现同样的验收场景，
并用 Anvil `evm_snapshot/evm_revert` 在用例间隔离状态。

## 端到端示例

```bash
# 先完成上面的「启动」三步（Anvil + deploy + uvicorn 不需要，示例直连链）
source .venv/bin/activate
python examples/demo.py
```

脚本演示：悬崖内领取被拒 → 推进到中点领取 50% → owner 撤销拿回另外 50% →
再推进时间，归属不再增加，最终 `released + refunded == 锁定总量`。

## 实测结果（2026-09-24，本机 Linux + Anvil/Foundry 1.8.3）

```
forge test        -> 15 passed; 0 failed
pytest tests/ -q  -> 9 passed（独立 Anvil :8547 + 快照隔离，连续多轮稳定通过）
examples/demo.py  -> released=500 refunded=500，守恒断言通过
HTTP curl 全流程  -> 悬崖内领取 400；中点撤销退 500；领取 500；
                     released + refunded == 1000，托管余额归 0
```

非整除用例（7 wei/1000s 与 3 wei/1000s）验证 dust 在终点结清，
链上与 HTTP 两层均断言「可领 + 返还 == 初始锁定量」。

## 安全与范围说明

- 仅面向本地 Anvil 测试：交易签名密钥为 Anvil 公开测试密钥，HTTP 服务无鉴权，
  **切勿部署到公链或暴露到非本机网络**。
- 金额/时间计算全部使用 uint256 checked arithmetic；乘法在除法之前，舍入方向为
  向下取整，最终 dust 在结束时刻结清。
