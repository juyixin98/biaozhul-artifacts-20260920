# 合约有界模型检查器 (Contract Bounded Model Checker, CBMC)

纯后端工具：对**显式 JSON 有限状态合约模型**做有界模型检查 (Bounded Model Checking)，
求解器为 [Z3](https://github.com/Z3Prover/z3)，HTTP 层为 FastAPI。
检查两类安全性质：

1. **余额非负** —— 每个 `int` 状态变量在可达状态上始终 `>= 0`；
2. **总额守恒** —— 所有 `int` 余额之和（模 2^64）恒等于初始总额。

核心承诺与边界：

- **只解释纯 JSON 数据结构**（条件、赋值），没有 `eval`/`exec`，不执行任意代码；
- 返回 `0..steps` 范围内的**最短反例状态序列**，并用独立的具体解释器**逐步重放交叉校验**；
- `counterexample` / `no_counterexample` / `timeout` / `unknown` 四种状态严格区分；
- **所有结论都限定在给定步数内**，任何返回都不会声称“已证明任意深度安全”。

> 整数语义：64 位有符号补码（与 Solidity `int64`/固定宽度机器字一致），
> 算术按位回绕，比较为有符号比较。

---

## 1. 目录结构

```
.
├── cbmc/
│   ├── model.py        # JSON 模型加载与递归校验（拒绝越权/非法结构）
│   ├── eval.py         # 纯数据 AST 具体解释器（64 位回绕语义）
│   ├── invariants.py   # 默认不变量：全部余额非负 且 总额守恒
│   ├── checker.py      # Z3 BMC 核心：位向量编码、迭代加深找最短反例
│   ├── replay.py       # 逐步重放求解器反例并逐字段交叉校验
│   ├── fixtures.py     # 内置夹具：安全转账/漏扣/溢出/两步泄漏/不可达
│   ├── api.py          # FastAPI: /health /fixtures /check /replay
│   ├── cli.py          # 命令行入口
│   └── __main__.py
├── examples/           # 示例模型输入 + 重放动作序列
├── tests/test_cbmc.py  # 35 个自动化测试
├── requirements.txt    # 顶层依赖
├── requirements.lock   # 完整锁定的传递依赖（精确版本）
└── pytest.ini
```

## 2. 本地启动

需要 Python 3.10+（开发环境为 3.12）。

```bash
cd /home/admin/Downloads/biaozhul/P030/a
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock     # 复现精确环境
# 或 .venv/bin/pip install -r requirements.txt

# 自动化测试
.venv/bin/python -m pytest

# 启动 HTTP 服务
.venv/bin/uvicorn cbmc.api:app --host 127.0.0.1 --port 8000
# 交互式 API 文档: http://127.0.0.1:8000/docs
```

## 3. 验收命令

服务启动后另开一个终端：

```bash
BASE=http://127.0.0.1:8000

# (1) 健康检查（含 Z3 版本，证明确实加载了真实求解器）
curl -s $BASE/health

# (2) 安全转账：给定步数内无反例（注意有界措辞，不声称任意深度安全）
curl -s -X POST $BASE/check -H 'Content-Type: application/json' \
  -d "{\"model\": $(curl -s $BASE/fixtures/safe_transfer),
       \"steps\": 6, \"timeout_ms\": 15000}" | python3 -m json.tool

# (3) 漏扣余额：深度 1 的最短反例
curl -s -X POST $BASE/check -H 'Content-Type: application/json' \
  -d "{\"model\": $(curl -s $BASE/fixtures/missing_debit),
       \"steps\": 5, \"timeout_ms\": 15000}" | python3 -m json.tool

# (4) 64 位溢出：守卫看似正确，收款方回绕成负数
curl -s -X POST $BASE/check -H 'Content-Type: application/json' \
  -d "{\"model\": $(curl -s $BASE/fixtures/overflow_transfer),
       \"steps\": 3, \"timeout_ms\": 15000}" | python3 -m json.tool

# (5) 步数边界：bound=1 无反例；bound=2 找到深度 2 的最短反例
curl -s -X POST $BASE/check -H 'Content-Type: application/json' \
  -d "{\"model\": $(curl -s $BASE/fixtures/late_leak),
       \"steps\": 1, \"timeout_ms\": 15000}" | python3 -m json.tool
curl -s -X POST $BASE/check -H 'Content-Type: application/json' \
  -d "{\"model\": $(curl -s $BASE/fixtures/late_leak),
       \"steps\": 2, \"timeout_ms\": 15000}" | python3 -m json.tool

# (6) 不可达动作：有界内未发现反例
curl -s -X POST $BASE/check -H 'Content-Type: application/json' \
  -d "{\"model\": $(curl -s $BASE/fixtures/unreachable_guard),
       \"steps\": 8, \"timeout_ms\": 15000}" | python3 -m json.tool

# (7) 逐步重放（不调用求解器，纯解释器执行）
curl -s -X POST $BASE/replay -H 'Content-Type: application/json' \
  -d '{"model": '"$(cat examples/missing_debit.json)"',
       "steps": [{"action": "withdraw", "params": {"amount": 8}}]}' | python3 -m json.tool
```

命令行同样可用（检查退出码：反例/无反例 `0`，超时/未知 `2`，模型非法 `1`）：

```bash
.venv/bin/python -m cbmc.cli check examples/missing_debit.json --steps 5
.venv/bin/python -m cbmc.cli check examples/safe_transfer.json  --steps 5
.venv/bin/python -m cbmc.cli replay examples/missing_debit.json \
  '[{"action":"withdraw","params":{"amount":8}}]'
```

## 4. 模型 DSL

```jsonc
{
  "name": "demo",
  "state_vars": [
    {"name": "alice",  "type": "int",  "init": 100},      // 64 位有符号整数
    {"name": "locked", "type": "bool", "init": false}
  ],
  "actions": [
    {
      "name": "transfer",
      "params": [{"name": "amount", "type": "int", "min": 0, "max": 1000}],
      "guards": [ /* 条件 AST 的合取；全部成立动作才可执行 */ ],
      "effects": [
        {"kind": "assign", "target": "alice", "expr": "/*算术 AST*/"},
        {"kind": "lock",   "target": "locked"},
        {"kind": "unlock", "target": "locked"}
      ]
    }
  ],
  "invariant": {"expr": "/*可选：自定义布尔 AST；默认非负+守恒*/"}
}
```

表达式 AST（只有数据，没有函数、字符串求值或任何可执行成分）：

| 种类 | 形态 |
|---|---|
| 整数常量 | `{"t":"num","value":7}` |
| 布尔常量 | `{"t":"bool","value":true}` |
| 变量 | `{"t":"var","name":"alice"}` |
| 算术 | `{"t":"arith","op":"+|-|*","args":[e,…]}`（64 位回绕） |
| 比较 | `{"t":"cmp","op":"<|<=|>|>=|==|!=","lhs":e,"rhs":e}`（布尔仅 `==/!=`） |
| 布尔 | `{"t":"boolop","op":"and|or|not","args":[…]}` |

语义约定：每个 effect 的右端都在**前置状态 + 参数**上同时求值后一次性提交
（标准 frame 公理，未赋值变量保持不变）。

## 5. `/check` 返回状态（四者严格区分）

| `status` | 含义 |
|---|---|
| `counterexample` | 在 `depth` 步找到不变量违例，`trace` 是经重放校验的最短状态序列 |
| `no_counterexample` | `0..bound` 全部可达状态上性质成立；**不代表任意深度安全** |
| `timeout` | 时间预算耗尽或 Z3 因 timeout 中断（含已检查到的深度信息） |
| `unknown` | Z3 返回 `unknown` 且原因不是超时（如 `incomplete theory`/资源中止） |

反例在返回前会被独立解释器从初始状态逐步重放：守卫必须成立、参数必须在声明
范围内、每一步状态必须与求解器模型逐字段相等、末态必须确实破坏不变量。
任何一项对不上都会报 `ReplayError`（HTTP 500），绝不返回无法复现的“反例”。

## 6. 夹具与测试覆盖

| 夹具 | 预期 |
|---|---|
| `safe_transfer` | 有锁 + 余额守卫 + 成对记账：有界内无反例 |
| `missing_debit` | 只加不扣：深度 1 破坏**守恒**（非负仍满足） |
| `overflow_transfer` | 余额守卫成立但收款方回绕为负：深度 1，由**非负**抓到 |
| `late_leak` | 必须先 `arm` 再 `leak`：最短反例恰为深度 2（步数边界） |
| `unreachable_guard` | 违例动作被矛盾守卫永久封锁：任意有界深度无反例 |

测试还覆盖：初始状态即非法（深度 0 反例）、篡改反例状态被重放拒绝、
参数越界/守卫不成立的重放报错、64 位模守恒、非法算符/未知变量/类型错误被
校验层拒绝（证明混入 `eval` 之类“算符”也不会被执行）、以及用替身 solver
精确区分 `timeout` 与 `unknown`。

## 7. 安全说明

- 输入模型是**不可执行的数据**：系统中没有 `eval`/`exec`/`pickle`/导入用户模块；
  未知算符、未声明变量、类型不匹配全部在 `model.py` 拒绝（HTTP 422）。
- 参数取值被模型显式声明的 `[min,max]` 约束；状态空间只在该范围内符号搜索。
- 求解结果必须通过具体重放才算数；失败一律如实报错，不静默降级。
