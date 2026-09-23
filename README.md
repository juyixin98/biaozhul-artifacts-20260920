# 合约有界模型检查器（Contract Bounded Model Checker）

一个**纯后端**的有界模型检查器：把显式 JSON 描述的有限状态合约展开成转移系统，
用 [Z3](https://github.com/Z3Prover/z3) 在用户给定步数内符号化地搜索违反
**余额非负**或**总额守恒**的执行，并返回**最短反例的具体状态序列**，
再用一套独立的 Python 具体语义**逐步重放**求解器给出的反例，确保它真实可执行。

不执行任何用户代码：表达式是带标签的 JSON，运算符来自固定白名单
（`+ - * neg`、`== != < <= > >=`、`and or not`、`ite`）。没有 `eval`、
没有字符串表达式、没有任意代码入口。

> 结果严格**有界**：`safe_within_bound` 只表示在给定步数内未找到反例，
> **绝不宣称任意深度安全**。超时、未知、未发现反例三者明确区分。

---

## 1. 它检查什么

| 性质 | 含义 |
|------|------|
| `nonnegative`（余额非负） | 每个声明为余额的整数变量在每一步都 `>= 0`（数学整数） |
| `conservation`（总额守恒） | 每个组内变量的（加宽）总和在每一步等于初始总额。对 8/… 位**机器整数**做零扩展后求和，因此 EVM/uint 式的加法回绕也会被抓到 |
| `targets`（命名目标） | 用户给定的任意布尔表达式在某状态可达（可表达「不可达状态」测试） |

## 2. 四种结果状态（必须区分，不可混淆）

| `status` | 含义 | 是否有 trace |
|----------|------|--------------|
| `counterexample` | 在 `depth` 步首次可达某个违例（迭代加深，保证**最短**） | 有 |
| `safe_within_bound` | 深度 `0..steps` 全部 UNSAT。**仅此而已，不是任意深度证明** | 无 |
| `timeout` | 某深度 Z3 在 `timeout_ms` 内未判定；更浅深度已确认干净 | 无 |
| `unknown` | Z3 因其它原因放弃（非线性整数等），返回 `UNKNOWN` | 无 |

每条结果都带一句 `note`，有界结果会显式写明 “NOT a proof of safety”。

---

## 3. 合约 JSON 语言（`cbmc-contract/v1`）

```jsonc
{
  "schema": "cbmc-contract/v1",
  "name": "...",
  "state": {
    "int":  { "alice": 10 },                       // 数学整数（无界、精确）
    "bv":   { "vault": {"width": 8, "init": 240}},// 无符号机器整数（模 2^w 回绕）
    "bool": { "locked": false }
  },
  "constants": { "FEE": 1 },
  "actions": [
    {
      "name": "transfer",
      "params": [
        {"name": "amount", "type": "int", "min": 0, "max": 10},
        {"name": "flag",   "type": "bool"},
        {"name": "word",   "type": {"type": "int", "width": 8}, "min": 0, "max": 255}
      ],
      "require": [ /* 布尔表达式，全部为真动作才启用 */ ],
      "assign":  [ /* 并行赋值，右端使用动作前状态 */ ]
    }
  ],
  "properties": {
    "balances": ["alice"],
    "conservation": [["alice", "bob"]],
    "targets": [{"name": "drained", "expr": { /* 布尔表达式 */ }}]
  }
}
```

表达式（全部是 JSON 对象，无字符串、无函数调用）：

- 整数：`{"lit": 7}`、`{"var": "alice"}`、
  `{"op":"+|−|*","args":[...]}`、`{"op":"neg","args":[e]}`
- 布尔：`{"bool": true}`、`{"var":"locked"}`、
  `{"op":"and|or","args":[...]}`、`{"op":"not","args":[e]}`、
  `{"op":"==|!=|<|<=|>|>=","args":[ie1,ie2]}`、
  `{"op":"ite","args":[cond,then,else]}`
- 语义：每一步**非确定地**选择一个已启用动作；`assign` 为**并行赋值**
  （右端都在动作前状态上求值），未列出的变量保持不变。
- 整数默认为数学整数；`state.bv` 或位宽参数为无符号机器整数，算术模 `2^w`
  回绕、比较为无符号比较。机器整数与数学整数变量不可混用（字面量会自动提升）。

---

## 4. 本地启动

需要 Python 3.11+（开发于 3.12）。

```bash
cd /home/admin/Downloads/biaozhul/P030/b

python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements-lock.txt     # 精确、可复现的依赖
# 或 pip install -r requirements.txt     # 直接依赖 + 浮动 pydantic

uvicorn cbmc.api:app --reload --port 8000
```

打开交互式文档：<http://localhost:8000/docs>

### 命令行（不启服务也能用）

```bash
python -m cbmc fixtures                                 # 列出内置夹具
python -m cbmc check --fixture safe-transfer --steps 20 # 安全（有界）
python -m cbmc check --fixture escrow-missing-debit --steps 5   # 最短反例+重放
python -m cbmc check --fixture uint8-pool-overflow --steps 5    # 机器整数溢出
python -m cbmc check examples/your_contract.json --steps 8
```

退出码：发现反例 `0`；有界安全/超时/未知 `1`；合约非法 `2`。

### HTTP 示例

```bash
# 健康检查
curl -s localhost:8000/healthz

# 列出 / 读取夹具
curl -s localhost:8000/api/fixtures
curl -s localhost:8000/api/fixtures/escrow-missing-debit

# 直接检查一个夹具（query 里给步数）
curl -s -X POST "localhost:8000/api/fixtures/escrow-missing-debit/check?steps=5"

# 提交任意合约 JSON
curl -s -X POST localhost:8000/api/check \
  -H 'content-type: application/json' \
  --data @examples/request_minimal.json

# 只单独重放一条 trace
curl -s -X POST localhost:8000/api/replay \
  -H 'content-type: application/json' \
  -d '{"contract": {...}, "trace": [ ... ]}'
```

`POST /api/check` 发现反例时会自动附带 `replay` 字段：独立的逐步具体执行、
与求解器状态逐变量比对、以及对每条性质的独立裁决。

---

## 5. 内置夹具（验收用）

| ID | 作用 |
|----|------|
| `safe-transfer` | 带布尔锁与余额守卫的安全转账；界内非负且守恒 |
| `escrow-missing-debit` | `settle()` 只加款给卖家、**漏扣**托管余额；第 2 步最短反例破坏守恒（凭空造钱） |
| `uint8-pool-overflow` | 8 位无符号整数加法回绕（240+x → 模 256）；加宽求和守恒检查抓到溢出 |
| `unreachable-target` | 一个命名目标**不可达**（UNSAT），另一个 1 步可达 |
| `piggy-step-boundary` | 目标恰需 15 次存钱+1 次打碎（深度 16）；验证步数边界与最短性 |

---

## 6. 自动化测试与验收命令

```bash
# 运行全部 41 个测试（真实调用 Z3 + 真实起 FastAPI app）
python -m pytest -q
```

覆盖：表达式排序/白名单拒绝、Z3 与具体求值一致性、位向量回绕、
五个夹具的性质、**最短反例深度**、**逐步重放且状态逐变量一致**、
篡改 trace（假守卫/状态不一致/空 trace）被拒、不可达状态 UNSAT、
机器整数溢出、步数上下界、以及超时/未知/有界安全的区分和 API 端到端。

手动验收一条完整链路（漏扣 → 最短反例 → 独立重放确认）：

```bash
python -m cbmc check --fixture escrow-missing-debit --steps 5 2>/dev/null \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); \
print(d["status"], d["depth"], d["violation"]); \
print("replay:", d["replay"]["executable"], d["replay"]["states_match_solver"], \
      d["replay"]["violated"])'
# 期望: counterexample 2 conservation:0
#       replay: True True ['conservation:0']
```

---

## 7. 项目结构

```
cbmc/
  model.py    # JSON 合约加载与严格校验（无任意代码）
  terms.py    # 表达式白名单：排序、Z3 编译、Python 具体求值（含位向量回绕）
  checker.py  # 转移展开、迭代加深最短反例、sat/unsat/unknown/timeout 区分
  replay.py   # 独立 Python 语义逐步重放，逐变量核对，独立裁决性质
  fixtures.py # 内置夹具注册表
  api.py      # FastAPI：/api/check、/api/replay、/api/fixtures*
  cli.py      # `python -m cbmc ...`
examples/     # 5 个夹具合约 + 一个 API 请求样例
tests/        # 41 个 pytest 测试
```

## 8. 实现要点（为什么 UNSAT 不会指数爆炸）

朴素「每动作一个 ITE 合并下一状态」的 BMC 编码会让守恒查询随深度指数级变慢。
本检查器对整数采用**按动作门控的净增量（gated net-delta）**编码：

- 每个动作对每个被赋值整数引入一个净增量符号 `d`；动作未选中时强制 `d=0`，
  选中时要求守卫成立且 `d == 右端表达式 − 旧值`；
- 下一状态是析取之外的线性方程 `x[k+1] = x[k] + Σd`。

于是守恒量按望远镜式线性传播，Z3 的 UNSAT 判定随深度近似线性增长
（同一安全模型深度 25 从 >12s 超时降到约 0.3–0.5s），而「漏扣」动作的净增量
天然不和为零，仍被如实检出。

## 9. 安全与诚实性说明

- 不加载、不 `eval`、不执行任何用户提供的代码；合约只是数据。
- 求解器模型在呈现为反例前，一定经过独立具体重放；重放与求解器状态不符会
  返回 `replay_error` / HTTP 422，而不是隐瞒。
- 计算（Z3 求解、具体重放）均在本机真实执行；超时与 `UNKNOWN` 如实上报，
  绝不把有界 UNSAT 表述成「已证明安全」。
