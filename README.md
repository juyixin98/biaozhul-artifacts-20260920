# Restricted Datalog Query Service

一个受限 Datalog 查询服务：支持**递归正规则**与**分层否定（stratified
negation）**，在编译期完成变量安全性与否定依赖环检查，采用**半朴素
（semi-naïve）不动点求值**，为每条答案返回**可核验的推导树**，并用 SQLite
持久化证据。所有递归与连接都在内核中完成，SQLite 只做证据存储，不参与推理。

## 快速开始

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# 一键本地验证（退出码 0 = 全部通过）
bash scripts/verify.sh

# 或手动
python3 -m pytest --cov=app --cov-report=term-missing
```

### CLI

```bash
python -m scripts.cli query   data/example.dl "ancestor(ann, X)"
python -m scripts.cli compile data/example.dl
python -m scripts.cli materialize data/example.dl
```

### HTTP 服务

```bash
python -m scripts.cli serve --host 127.0.0.1 --port 8000
# 或: uvicorn app.api.server:app --port 8000

curl -s -X POST localhost:8000/query -H 'Content-Type: application/json' -d '{
  "program": "parent(ann,bob). parent(bob,cy).\nancestor(X,Y):-parent(X,Y).\nancestor(X,Y):-parent(X,Z),ancestor(Z,Y).",
  "goal": "ancestor(ann,X)",
  "request_id": "demo-1"
}'
```

端点：`POST /query`、`POST /compile`、`POST /materialize`、
`GET /requests/{id}`、`GET /requests/{id}/summary`、
`GET /requests/{id}/derivation?pred=..&args=[..]`、`GET /health`。

环境变量：`DATALOG_DB_PATH`（默认 `data/evidence.db`）、
`DATALOG_PROGRAM_ID`、`DATALOG_LOG_LEVEL`、`DATALOG_LOG_FILE`。

## 语言

```
fact := atom "."
rule := atom ":-" literal ("," literal)* "."
literal := ["NOT"] atom | term cmp term
cmp   := "=" | "!=" | "<" | "<=" | ">" | ">="
```

* 大写开头的标识符是变量（`X`、`Who`），其余为常量（`ann`、`12`）；
  `_` 为存在量词通配符；`"..."` 为带空格的字符串常量。`%` 起始为注释。
* 比较运算在两侧都像数字时按数值比较，否则按字符串比较。

## 算法假设与语义

1. **集合语义（set semantics）**：事实与派生元组去重；同一元组即使可由
   多条规则/多次触发得到，也只保存一条，证据取**字典序最小的首次触发**
   作为规范推导（canonical derivation）。版本指纹（SHA-256，对规范渲染
   排序后计算）与规则书写顺序、文本布局无关。
2. **范围受限安全性（range restriction）**：出现在规则头、否定文字或比较
   中的每个变量，都必须被某个**正关系**文字绑定；否则编译期以
   `unsafe_variable` 拒绝（避免否定/比较产生自由变量 floundering）。
   通配符 `_` 是否定中的存在量词，不要求出现在正文字中。
3. **分层否定**：在谓词依赖图上求 Tarjan SCC；任何 SCC 内含否定边即无分
   层模型，编译期以 `negation_cycle` 拒绝。SCC 的层号为
   `max(依赖层 + 是否定边?1:0)`，纯正递归留在同一层。
4. **半朴素不动点**：每轮只有用上一轮增量（delta）的触发能产生新元组。
   bootstrap 轮全量扫描（此时递归谓词为空）；之后对规则的每个递归正文字
   生成一个变体——该文字扫 delta，其余扫全量。逐层求值至不动点。
5. **完备模型**：在安全、可分层前提下，求唯一的分层最小模型（perfect
   model）。与独立的朴素求值器结果一致。

## 模块关系

```
app/
  language/   词法 lexer.py → 语法 parser.py → terms.py(AST)
              compiler.py  : arity / 安全性 / SCC 分层 / 版本指纹
              errors.py    : 稳定的机器可读失败类别
  engine/
    relations.py : 内存关系、嵌套循环连接、否定/比较、产生 Firing（证据）
    fixpoint.py  : 分层 + 半朴素不动点；记录每条派生元组的规范 Firing
    naive.py     : 独立的朴素参考求值器（全量重扫，仅在测试中充当 oracle）
    proof.py     : 由 Firing 重建 fact/rule/absence 证明树
  store/
    sqlite_store.py : 证据持久化（programs/requests/derivations/support）
  service.py  : 编排 compile→evaluate→answer→record，生成可解释响应
  api/server.py : FastAPI 薄层
  config.py / logging_setup.py
tests/        fixtures_programs.py 为手工夹具（答案由人工预先算出）
              parser / compiler / engine / service / api / 等价性属性测试
```

关键边界：`store` 从不被推理调用；内核不 import store。答案不依赖被测内核
自我生成——测试同时断言**手工集合**、与**独立朴素求值器**一致、以及规则
重排后闭包不变。

## 可解释性

每个响应都带：`request_id`、程序 `version`、`strata`（每层谓词/迭代数/产
出数）、`steps`（每层每轮增量与落库位置）、每条答案的 `proof` 证明树
（叶为 EDB 事实，否定以 `absence` 节点给出“无任何匹配元组”的模式，`*`
为存在位置）、以及单列的 `failures`（类别+原因）与 `uncertainty`。服务端
日志为 key=value，每行带 `request_id=` 以便关联。

## 测试与预期判断

| 测试文件 | 断言内容 |
|---|---|
| `test_parser.py` | 词法/语法、变量与常量、通配符、非基事实拒绝 |
| `test_compiler.py` | `unsafe_variable` / `negation_cycle` / `arity_mismatch` 类别、分层号、版本稳定性 |
| `test_engine.py` | 祖先/递归排除等**手工闭包**、半朴素=朴素、规则顺序无关、增量收缩、证据完整、集合去重 |
| `test_equivalence_properties.py` | 43 个随机程序上半朴素=朴素、规则重排闭包相同 |
| `test_service.py` | 绑定结果、证明树、失败类别、落库回读 |
| `test_api.py` | HTTP 端到端、404/400/422、SQL 支撑行回查 |

判断方式：`bash scripts/verify.sh` 全绿（退出码 0）、总覆盖率 ≥ 80%
（当前约 95%）。手工夹具中的期望集合（如祖先 12 对、`sink={d}`、
`unreachable={a,d}`）在 `tests/fixtures_programs.py` 中显式列出。

## 依赖版本（本机实测）

Python 3.12.3；fastapi 0.141.1；pydantic 2.13.5；uvicorn 0.54.0；
httpx 0.28.1；pytest 9.1.1；pytest-cov 7.1.0。SQLite 使用标准库
`sqlite3`（驱动随解释器提供）。无外部数据库、无网络依赖。

## 范围与限制

* 非 Datalog 的过程式特性（聚合、更新、函数项）不在范围内。
* 嵌套循环连接面向小规模式合成夹具，非查询优化器；无索引/魔法集。
* 否定为分层否定，不支持局部否定（well-founded / answer-set 语义）。
