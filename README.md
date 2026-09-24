# 表达式污点分析服务 (TaintLang Static Taint Analysis)

纯后端的小型脚本语言**静态污点分析 HTTP 服务**。语言包含赋值、分支、循环、
用户自定义函数以及预设的 `source()` / `sink()` / `sanitize()`。分析器使用
**单调不动点**（函数内乱序 worklist + 函数间 SCC 迭代）计算 may-taint 解，
并为每个 source→sink 告警输出一条**最短证据路径**。报告使用 `cryptography`
（Ed25519）签名，客户端可用服务公钥验签。

## 1. 目录结构

```
app/
  lexer.py        词法分析
  ast_nodes.py    AST 定义
  parser.py       递归下降解析
  analyzer.py     CFG + 不动点数据流分析 + 函数摘要/SCC 递归处理
  crypto_sign.py  Ed25519 报告签名/验签（cryptography）
  web.py          FastAPI HTTP 接口
tests/
  test_analyzer.py    25 个自动化测试
  fixtures/*.tl       跨函数、条件净化、递归、循环等夹具
examples/
  requests.sh     curl 请求样例
  client.py       Python 客户端样例（调用接口 + 验签 + 打印证据链）
requirements.txt  锁定依赖（pip freeze 全量版本）
RUN_RESULTS.md    测试与示例的实际运行记录
```

## 2. 依赖与启动

要求 Python 3.12（3.10+ 亦可）。全部依赖版本见 `requirements.txt`，关键版本：

| 包 | 锁定版本 |
|---|---|
| fastapi | 0.141.1 |
| starlette | 1.7.0 |
| uvicorn | 0.53.0 |
| pydantic / pydantic_core | 2.13.5 / 2.46.5 |
| cryptography | 50.0.1 |
| httpx（测试/客户端） | 0.28.1 |
| pytest | 9.1.1 |

安装与启动：

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

.venv/bin/uvicorn app.web:app --host 127.0.0.1 --port 8000
# 交互式 API 文档: http://127.0.0.1:8000/docs
```

服务首次启动时在 `.data/ed25519_key.pem`（可用环境变量 `TAINT_KEY_DIR`
改目录）自动生成签名私钥，权限 600。

运行自动化测试：

```bash
.venv/bin/python -m pytest -q
```

## 3. TaintLang 语言

无类型、只有整型/字符串/布尔/nil 字面量、一级函数（无嵌套函数、无全局变量、
无指针/数组/对象，因此字段敏感性问题不存在）。

```
program   := (funcdef | statement)*            # 顶层语句即程序入口
funcdef   := 'func' NAME '(' params? ')' block
block     := '{' statement* '}'
statement := block
           | 'if' '(' expr ')' statement ('else' statement)?
           | 'while' '(' expr ')' statement
           | 'return' expr? ';'
           | NAME '=' expr ';'                 # 赋值即声明，函数作用域
           | expr ';'
expr      := || / && / 比较 / 加减 / 乘模 / 一元 !,- / 初等
primary   := INT | STRING | true | false | nil
           | NAME '(' args? ')' | NAME | '(' expr ')'
```

内建函数（不可重定义，实参个数严格校验）：

- `source()` → 攻击者可控数据（污点源，0 参）
- `sanitize(x)` → 净化，返回值**不含**任何污点（1 参）
- `sink(x)` → 危险汇聚点；实参带污点则告警（1 参）

注释用 `#` 到行尾。分号必填。

## 4. 分析模型（上下文敏感程度 / 不动点 / 误报边界）

**抽象域**：每个函数内为 `变量 → {污点来源 token: 证据路径}`，
join 用集合并（may-taint）。来源 token 有两类：

- `src:<ast-id>`：某个具体 `source()` 调用点；
- `param:<函数名>:<i>`：函数第 i 个形参的**符号来源**。

**函数内**：先构建 CFG（entry / stmt / branch / while_header / skip / exit，
`return` 直连 exit），再用混沌 worklist 迭代到最小不动点。赋值是强更新；
分支两个出口、`while` 回边与出口都以相同环境传播，因此**循环携带污点**
（见 `tests/fixtures/loop_fixpoint.tl`：污点在第 4 轮才出现）无需展开即可得到。

**函数间**：为每个用户函数计算一份**token 参数化摘要**
`(返回值污点来源→证据, 各 sink 点的污点来源→证据)`。调用点把实参的具体来源
**替换**到被调函数形参的符号 token 上（参数化 0-CFA）。这样：

- 同一函数被干净/脏两种实参分别调用时，只有脏调用点产生告警
  （`test_same_function_called_clean_and_dirty`）；
- source 可以在被调函数内、sink 在调用方，反之亦然（`source_in_callee.tl`）；
- 不存在按调用串展开导致的指数爆炸。

**上下文敏感程度（明确说明）**：

- **流敏感**（flow-sensitive，按 CFG 顺序、强更新）；
- **上下文不敏感（0-CFA）但参数来源精确**：函数只分析一次，形参用符号 token
  表示，调用点做实参替换。不区分调用串，同一函数多个调用点的副作用在摘要层
  合并；对本语言（无全局/堆）这不会产生额外误报；
- **路径不敏感**（path-insensitive）：不跟踪条件取值，if 两臂在 join 处并集；
- **域不敏感/字段不敏感**：语言无复合结构，天然成立；
- **不跟踪隐式流**（`if (secret) x = 1` 不会把 x 标记为脏）。

**递归**：Tarjan SCC 求调用图强连通分量，按被调者优先顺序处理；对递归 SCC
从空摘要开始反复重分析，直到所有成员摘要的签名不再变化。token 全集有限
（source 调用点 + 形参的数量固定），且同一来源的证据路径只在**严格变短**时
替换，因此迭代单调有界、必然终止（`MAX_SCC_ITERATIONS=10000` 兜底）。
`tests/fixtures/recursion.tl` 的最短证据路径必须穿过递归环，需要 3 轮
不动点才能发现。

**误报边界（如实声明）**：

1. **单臂净化报为危险**：污点只在 `if` 的一个分支被 `sanitize()`，另一分支
   仍脏，join 后变量 may-taint → **报告**（may 分析的固有不精确，
   见 `conditional_sanitize_fp.tl`）。所有臂都净化则安全
   （`conditional_sanitize_safe.tl`）。
2. **不解释常量条件**：即使条件是 `true`/`false` 也不做常量折叠，两条边都走。
3. **隐式流不跟踪**（只做显式数据流）。
4. 证据路径是“最短的一条”，路径不敏感时该路径可能对应不可行路径
   （但来源、传播关系真实存在）。
5. **不可达不报告**：从未被入口调用的函数，其内部 source→sink 不出现在结果中
   （按可达的执行路径计）。

在上述语言语义与显式数据流范围内，分析对**赋值、所有运算符、函数实参/返回值、
分支合并、循环、互递归**的传播是可靠的（设计上无漏报）。

## 5. HTTP 接口

### `GET /health` → `{"status":"ok",...}`

### `GET /public-key`
返回 Ed25519 公钥 PEM。

### `POST /analyze`
请求：

```json
{ "code": "x = source();\nsink(x);\n" }
```

响应（危险样例，节选）：

```json
{
  "report": {
    "ok": true,
    "language": "TaintLang-1",
    "vulnerable": true,
    "finding_count": 1,
    "findings": [
      {
        "source": {"func": "<main>", "loc": "1:5"},
        "sink":   {"func": "<main>", "loc": "2:1"},
        "path_length": 3,
        "call_chain": ["<main>::sink"],
        "path": [
          {"kind": "source", "func": "<main>", "loc": "1:5",
           "detail": "source() produces attacker-controlled data"},
          {"kind": "assign", "func": "<main>", "loc": "1:1",
           "detail": "assigned to variable 'x'"},
          {"kind": "sink", "func": "<main>", "loc": "2:1",
           "detail": "tainted value reaches sink(x)"}
        ]
      }
    ],
    "stats": {"functions": 1, "user_functions": 0,
              "scc_fixpoint_rounds": 0, "sources": 1,
              "sinks_hit": 1, "findings": 1},
    "analysis": { "kind": "...", "context_sensitivity": "...",
                  "fixpoint": "...", "false_positive_boundary": "..." }
  },
  "signature": "<base64 Ed25519 over canonical JSON of report>"
}
```

安全样例：`{"code": "x = source(); x = sanitize(x); sink(x);"}`
→ `report.vulnerable === false`、`findings: []`。

词法/语法/语义错误返回 `200` 与
`{"ok": false, "error": {"type": "ParseError", "message": "..."}}`
（类型为 `LexError` / `ParseError` / `AnalysisError`，消息带行列号）。

证据步骤 `kind` 取值：`source | assign | op | call | param | return | sink`。

### `POST /verify-signature`
请求 `{"report": {...}, "signature": "..."}` → `{"valid": true|false}`。

验签的规范序列化：`json.dumps(report, sort_keys=True, separators=(",",":"))`
的 UTF-8 字节，Ed25519。

### curl 样例

```bash
curl -s -X POST http://127.0.0.1:8000/analyze \
  -H 'Content-Type: application/json' \
  -d '{"code": "x = source();\nsink(x);\n"}'

# 从夹具文件发请求
jq -Rs '{code: .}' tests/fixtures/recursion.tl \
  | curl -s -X POST http://127.0.0.1:8000/analyze \
    -H 'Content-Type: application/json' -d @-
```

更多见 `examples/requests.sh`；带验签和证据链打印的客户端：

```bash
.venv/bin/python examples/client.py tests/fixtures/recursion.tl
```

## 6. 验收夹具

| 夹具 | 预期 | 覆盖点 |
|---|---|---|
| `cross_function.tl` | 危险 | 跨 2 个函数、经实参与返回值传播，13 步证据路径 |
| `source_in_callee.tl` | 危险 | source 在被调函数内、sink 在顶层 |
| `conditional_sanitize_fp.tl` | **报告（误报边界）** | 仅单臂净化，路径不敏感 |
| `conditional_sanitize_safe.tl` | 安全 | 两臂都净化 |
| `loop_fixpoint.tl` | 危险 | 循环携带污点，依赖不动点 |
| `recursion.tl` | 危险 | 互递归 SCC，3 轮不动点后路径穿环 |
| `safe.tl` | 安全 | 常量运算，无 source |

## 7. 未完成 / 不做的范围

- 仅纯后端，无界面（`/docs` 的 Swagger 是框架自带，非自绘界面）。
- 语言刻意很小：无数组/结构体/全局变量/闭包/字符串污点建模，
  因此没有字段敏感性、堆建模与过程间别名问题。
- 不做常量条件判定、不可达分支删除与路径敏感（按设计，属误报边界）。
- 不跟踪隐式流（控制依赖传播）。
- 未做限流/鉴权；签名只保证“报告确由本服务私钥签发、未被篡改”，不加密。
- 多线程下每次 `/analyze` 的分析状态独立；签名私钥在启动时加载一次。
