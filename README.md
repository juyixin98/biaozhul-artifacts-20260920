# 有界订单签名结算（Bounded Signed-Order Settlement）

仅用于**本地测试链**的签名兑换系统：maker 对订单做 EIP-712 链下签名，taker 提交签名到结算合约成交。
预置两种模拟代币（TKA / TKB），支持**部分成交、nonce 取消、订单到期、按订单累计的费用上限**，
签名域绑定 **chainId 与合约地址**。

- 合约：Solidity `0.8.26` + Foundry（forge / anvil）
- 服务：Python 3.12 + FastAPI + web3.py，HTTP 接口连接**本机 Anvil**
- 全部账户均为 Anvil 公开测试密钥，无私募资金，切勿用于主网。

---

## 1. 目录结构

```
src/
  BoundedSettlement.sol   # 结算合约：EIP-712 验签、部分成交、取消、到期、费用上限
  MockERC20.sol           # 模拟代币：公开 mint、转账失败开关
test/
  BoundedSettlement.t.sol # Foundry 单元测试（20 个，零外部依赖，手写 Vm 接口）
app/
  main.py                 # FastAPI：/orders/sign /settlements /cancellations /orders/{hash} /balances /health
  signing.py              # EIP-712 签名（与合约 typehash 严格一致）
  errors.py               # 合约自定义错误 revert data 解码
  config.py               # 从 deployment.json 读取地址/账户
scripts/
  deploy.py               # 在本地 Anvil 部署合约 + 铸币 + 授权，生成 deployment.json
  demo.py                 # 端到端示例（走 HTTP 路由 + 链上余额守恒校验）
tests/
  conftest.py             # pytest fixture：自动起 anvil、forge build、部署
  test_settlement.py      # 端到端集成测试（15 个）
foundry.toml
requirements.in           # 直接依赖（宽松约束）
requirements.txt          # 锁定的精确版本（pip freeze，53 个包）
```

## 2. 依赖

### 系统
- [Foundry](https://book.getfoundry.sh/getting-foundry/installation)（forge、anvil；已用 `1.8.3` / solc `0.8.26` 验证）
- Python 3.12（含 venv、pip）

### Python（精确版本见 `requirements.txt`）
- fastapi、uvicorn（HTTP 服务）
- web3 8.x、eth-account（链交互与 EIP-712 签名）
- pydantic v2、httpx
- pytest（集成测试）

## 3. 安装

```bash
# Foundry（如尚未安装）
curl -L https://foundry.paradigm.xyz | bash
foundryup

# Python 依赖（建议使用 venv）
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

> `requirements.txt` 是当前环境锁定版本；需要重新求解时可改装 `requirements.in` 后再 `pip freeze > requirements.txt`。

## 4. 启动命令（手动运行）

需要 **3 个终端**（或用第 5 节的一键自动化测试，无需手动起链）。

### 终端 A：本地链

```bash
anvil --host 127.0.0.1 --port 8545 --chain-id 31337
```

### 终端 B：编译并部署

```bash
source .venv/bin/activate
export PATH="$HOME/.foundry/bin:$PATH"

forge build                              # 产物输出到 out/
BOUNDED_RPC_URL=http://127.0.0.1:8545 python scripts/deploy.py
# 生成 deployment.json（地址 + Anvil 测试私钥），并完成铸币 / approve
```

### 终端 C：HTTP 服务

```bash
source .venv/bin/activate
BOUNDED_RPC_URL=http://127.0.0.1:8545 uvicorn app.main:app --host 127.0.0.1 --port 8000
```

服务默认从项目根的 `deployment.json` 读取配置；可用环境变量 `BOUNDED_RPC_URL` 覆盖 RPC、
`BOUNDED_DEPLOYMENT_PATH` 覆盖部署清单路径。

## 5. 自动化测试

### Foundry 合约测试（自带 EVM，无需起链）

```bash
export PATH="$HOME/.foundry/bin:$PATH"
forge test -vv
```

### Python 端到端测试（自动起临时 Anvil、编译、部署、跑 HTTP）

```bash
source .venv/bin/activate
export PATH="$HOME/.foundry/bin:$PATH"
python -m pytest tests/ -v
```

fixture 会在空闲端口启动一次性的 `anvil`，执行 `forge build` 与 `scripts/deploy.py`，
结束后自动关闭节点，不影响 8545 上的任何实例。

### 端到端示例（需要先按第 4 节起链并部署）

```bash
BOUNDED_RPC_URL=http://127.0.0.1:8545 python scripts/demo.py
```

## 6. HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 链 ID、最新区块、合约与代币地址 |
| POST | `/orders/sign` | 用指定角色私钥对订单做 EIP-712 签名，返回 order / signature / order_hash |
| POST | `/settlements` | taker 提交 `{order, signature, sell_fill_amount, fee_amount}` 结算 |
| POST | `/cancellations` | maker 取消 `nonce` |
| GET | `/orders/{order_hash}` | 查询订单已成交量（sell / buy / fee 累计） |
| GET | `/balances?address=` | 查询某地址 TKA / TKB 余额 |

合约回滚统一返回 **HTTP 409**，`detail` 中是解码后的自定义错误，例如
`合约回滚: FillExceedsRemaining(1, 0)`、`NonceAlreadyCancelled(0x7099…, 43)`、
`OrderExpired(…)`、`FeeCapExceeded(2, 1)`、`TransferFailed(token,from,to,amount)`、
`InvalidSignature(recovered,maker)`。

### curl 速览

```bash
curl -s http://127.0.0.1:8000/health

SIGN=$(curl -s -X POST http://127.0.0.1:8000/orders/sign -H 'content-type: application/json' -d '{
  "signer":"maker","sell_token":"A","buy_token":"B",
  "sell_amount":5,"buy_amount":17,"fee_cap":2,"nonce":1001}')

# 部分成交 2/5，费用 1
curl -s -X POST http://127.0.0.1:8000/settlements -H 'content-type: application/json' \
  -d "$(python -c 'import json,os;s=json.loads(os.environ["SIGN"]);print(json.dumps({"order":s["order"],"signature":s["signature"],"sell_fill_amount":2,"fee_amount":1}))')"
```

## 7. 关键语义

### 订单结构（EIP-712）

```
Order(address maker, address sellToken, address buyToken,
      uint256 sellAmount, uint256 buyAmount,
      uint256 feeCap, uint256 nonce, uint256 expiry)
```

- `sellAmount` 是 **maker 卖出额度的硬上界**，`buyAmount` 是完全成交时 taker 应付总量。
- 域分隔符：`EIP712Domain(name, version, chainId, verifyingContract)`，构造时固化，
  换链或换合约地址签名即失效。

### 部分成交与舍入

- 中间各笔：`buyPaid = floor(buyAmount * sellFill / sellAmount)`（向下取整）。
- **最后一笔**（把剩余额度一次吃完）：`buyPaid = buyAmount - 已支付累计`，补足差额。
  因此完全成交时 maker 收到的 buyToken **恰好**等于签名的 `buyAmount`，
  任何一笔都不会让累计成交量超过 `sellAmount` / `buyAmount`。
- 例：`3 A -> 100 B` 拆成 1/1/1，三笔付款为 **33 / 33 / 34**。

### 取消 / 到期 / 费用

- `cancelNonce(nonce)` 按 maker 维度标记；取消后所有带该 nonce 的签名立即失效，
  已成交部分保留（这是“成交与取消竞争”的确定性规则）。
- `block.timestamp > expiry` 时拒绝（`== expiry` 仍可成交）。
- 费用以 buyToken 计、由 taker 支付给 `feeReceiver`，**按订单累计**且 `<= feeCap`，超限即回滚。

### 安全要点

- 先更新成交状态再做外部转账（CEI）；任一 `transferFrom` 失败（返回 false 或 revert）整笔回滚。
- 验签拒绝非 65 字节签名、非法 `v`、EIP-2 高位 `s`；recover 地址必须等于 `maker`。
- 成交额度、费用上限全部在链上校验，taker 无法通过参数越权。

## 8. 测试与示例的实际运行结果

以下结果为本机**实际执行**所得（Foundry 1.8.3 / solc 0.8.26 / Python 3.12 / web3 8.0.0）。

- `forge test`：**20 passed, 0 failed**。
  覆盖：完全成交+费用、两次部分成交舍入（33/33/34）、多笔不超额度、超量回滚、
  完全成交后重放、先取消后成交、先部分成交再取消、到期与到期边界、费用累计上限、
  sellToken/buyToken 转账失败回滚、错误签名人、错误签名长度、非法 v、高位 s、
  签名域绑定合约（A 合约签名在 B 合约被拒）、篡改订单、奇数比例守恒、重复取消。
- `pytest tests/`：**15 passed**，连续运行 3 次结果一致。
  覆盖与上面相同的验收场景，并额外校验本地 EIP-712 摘要与链上 `hashOrder` 一致、
  HTTP 409 错误解码、以及两种代币在 maker/taker/feeReceiver 三方的总量守恒。
- `scripts/demo.py`：在全新 Anvil 上全部场景通过。
  关键输出：`3 A -> 100 B` 三笔部分成交 buyPaid = **[33, 33, 34]**；
  重放 → `FillExceedsRemaining(1, 0)`；取消竞争 → `NonceAlreadyCancelled(..., 43)`；
  到期 → `OrderExpired(...)`；超费 → `FeeCapExceeded(2, 1)`；
  转账失败 → `TransferFailed(...)` 且回滚后 filled=0、余额不变。
- 真实 uvicorn（非 TestClient）+ curl 验证：`5 A -> 17 B`，
  成交 2 得 6、成交 3（末笔）得 11，合计 17；maker(-5 A,+17 B)、
  taker(+5 A,-19 B)、fee(+2 B)，再提交返回 `FillExceedsRemaining(1, 0)`。

### 开发过程中实际遇到并修复的问题（如实记录）

1. Solidity 保留字 `after` 不能作变量名 → 改名 `aft`。
2. 合约函数局部变量过多导致 `Stack too deep` → `foundry.toml` 开启 `via_ir`。
3. Foundry 测试中 `vm.sign` 放在 `vm.expectRevert` 之后会“消耗掉”回滚期望
   → 改为先签名、再 `expectRevert`、最后提交。
4. Python 服务最初构建交易时漏调 `build_transaction`，交易变成无 `to` 的合约创建交易
   → 修复后成交与事件正常。
5. web3 v8 的回滚异常是 `ContractCustomError`、事件用 `process_log` 按地址过滤解析。
6. “到期”用例在墙钟 `now-1` 时存在跨秒边界抖动 → 改为 `now-3600`；
   默认 expiry 改为基于**链上时间**，对被 warp 过的节点也健壮。

## 9. 未完成项 / 已知边界

- 仅面向本地测试链：`MockERC20` 的 `mint` 完全公开、`setTransfersFail` 任何人可切换，
  **不可用于生产**；生产代币应使用标准 ERC20。
- 订单模型刻意保持精简：无链下订单簿 / 索引服务、无 maker 委托签名（EOA 直接签名）、
  无 EIP-1271 合约钱包验签、无 ERC20 转账费（fee-on-transfer）代币适配。
- 未做 gas 优化审计与模糊测试（fuzz/不变量测试可作为后续增强）。
- HTTP 服务无鉴权 / 速率限制（本地可信环境假设）；节点仅监听 127.0.0.1。
