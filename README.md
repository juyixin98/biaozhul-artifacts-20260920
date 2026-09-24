# 表达式污点分析服务（Expression Taint Analysis）

纯后端的小型脚本语言**静态污点分析** HTTP 服务。内置词法/语法分析、显式
控制流图（CFG）、跨函数的**调用串（call-string）上下文敏感**不动点求解，
并为每个 source→sink 报告一条可读的**证据路径**。报告使用
`cryptography`（Ed25519）签名，可离线验签以防结论在传输中被篡改。

- 语言：赋值、`if/else`、`while`、函数（含递归/相互递归）、预设 source /
  sanitizer / validator / sink / propagator
- 框架：Python 3.10+ / FastAPI / Pydantic / cryptography
- 算法：单调框架（monotone framework）+ 工作流不动点，流敏感 + 有界上下文敏感

---

## 1. 目录结构

```
app/
  language/          # 前端：lexer.py、ast_nodes.py、parser.py、walk.py
  analysis/
    taint.py         # 三值污点格 CLEAN / MAYBE / TAINT
    evidence.py      # Value / Chain / Hop（证据链，带长度上界保证收敛）
    policy.py        # 内置 source/sink/sanitizer/validator/propagator 模型
    constraints.py   # 条件净化（validator 守卫精化）
    constant_fold.py # 常量分支消除（if(true)/while(false) 等）
    cfg.py           # 每个函数的控制流图（带回边标记）
    engine.py        # 跨函数调用串不动点引擎
    service.py       # 外观层：源码 -> 结构化报告
  signing.py         # Ed25519 报告签名/验签（cryptography）
  main.py            # FastAPI 应用
tests/               # pytest（格/解析/分析夹具/HTTP 端到端，共 45 例）
examples/            # 危险、安全、条件净化、递归、循环样例
scripts/             # CLI 演示与 curl 样例
requirements.txt     # 直接依赖（精确版本）
requirements.lock    # 全量传递依赖锁定（运行时）
requirements-dev.lock# 含测试依赖的全量锁定
```

## 2. 安装与启动

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
# 启动服务
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

开发/测试安装：

```bash
pip install -r requirements.txt pytest==8.3.* httpx==0.27.*
# 或：pip install -e ".[test]"
```

> 锁定文件 `requirements.lock` 记录了在 Python 3.12 上验证通过的全量传递依赖。
> 如需可复现安装：`pip install -r requirements.lock`。

## 3. 语言速览

```
func name(params) { stmt* }
stmt := var x [= expr]; | x = expr; | return [expr];
      | if (cond) { } [else { }] | while (cond) { } | expr;
expr := 整数 | "字符串" | true | false | nil | 变量
      | 函数调用 f(...) | 算术 + - * / % | 比较 == != < <= > >=
      | 逻辑 and / or / not / ! | 括号
```

入口函数默认 `main`，可通过请求字段 `entry` 指定。`//` 行注释与
`/* ... */` 块注释均支持。

### 内置安全策略（默认）

| 类别 | 名称 | 语义 |
|---|---|---|
| source | `input` `read_file` `get_env` `recv` | 返回值恒为污点 |
| sanitizer | `escape` `sanitize` `int_cast` `encode` | 返回值恒为干净（数据清洗） |
| validator | `validate` | 返回干净布尔；**为真分支**上其参数被视为可信 |
| propagator | `concat`（全部参数）、`wrap`/`echo`/`str_cast`（首参） | 参数污点传播到结果 |
| sink | `sink` `exec` `eval` `query` `send` | 参数被检查 |
| pure | `len` | 恒干净 |

请求可通过 `sources / sanitizers / validators / sinks / propagators` 字段
**增量扩展**策略。注意：内置名优先于同名用户函数（因此示例中的用户函数命名为
`wrapper` 而非内置的 `wrap`）。

## 4. HTTP 接口

### `GET /health`
健康检查。

### `GET /signing-key`
返回 Ed25519 公钥（PEM），用于离线验证报告签名。

### `POST /analyze`
请求体：

```json
{
  "code": "func main() { sink(input()); }",
  "entry": "main",
  "sources": [], "sanitizers": [], "validators": [], "sinks": [],
  "propagators": {"myconcat": []},
  "call_string_k": 3
}
```

响应（节选）：

```json
{
  "verdict": "vulnerable",
  "finding_count": 1,
  "findings": [{
    "sink": "sink", "function": "main", "line": 1, "col": 19,
    "argument": 0,
    "taint_level": "tainted",
    "certainty": "definite",
    "evidence_paths": [{
      "source": "input", "truncated": false,
      "hops": [ ... ],
      "rendered": "main@1: source(input) -> main@1: sink(sink)"
    }]
  }],
  "context_sensitivity": {
    "kind": "call-string (context-sensitive, flow-sensitive)",
    "call_string_k": 3,
    "contexts": ["<entry> > main"]
  },
  "reachable_functions": ["main"],
  "fixpoint_iterations": 3,
  "warnings": [],
  "signature": { "algorithm": "Ed25519", "canonical_json": "...",
                 "signature_b64": "...", "public_key_pem": "-----BEGIN ..." }
}
```

### `POST /verify`
请求 `{"report": <完整报告>}`，返回 `{"valid_signature": bool, "verdict": ...}`，
用于校验报告确实由本服务签发且未被篡改。

### curl 样例

```bash
curl -s http://127.0.0.1:8000/health
curl -s -X POST http://127.0.0.1:8000/analyze \
  -H 'Content-Type: application/json' \
  -d '{"code": "func main() { sink(input()); }"}'
```

`scripts/curl_examples.sh` 提供对 examples 目录各文件的完整请求示例；
无需 HTTP 的命令行用法：

```bash
python scripts/analyze_file.py examples/dangerous.tl
```

## 5. 分析方法（算法说明）

### 5.1 污点格

内部使用四值格；`BOTTOM` 不出现在报告里，报告只显示 `clean / maybe / tainted`。

| 级别 | 含义 |
|---|---|
| **BOTTOM** | 无信息：字面量常量、未绑定的形参、尚未分析到的用户调用返回值（join 中性元） |
| **CLEAN** | 有来源且可证干净：字面量初始化、sanitizer 结果、validator 真分支上的值 |
| **MAYBE** | 汇聚路径不一致：一条干净、一条污点（条件/循环相关） |
| **TAINT** | 所有汇聚路径都污染 |

控制流合并 `join`（边/分支/循环汇聚）：

| join | BOTTOM | CLEAN | MAYBE | TAINT |
|---|---|---|---|---|
| **BOTTOM** | BOTTOM | CLEAN | MAYBE | TAINT |
| **CLEAN** | CLEAN | CLEAN | MAYBE | **MAYBE** |
| **MAYBE** | MAYBE | MAYBE | MAYBE | MAYBE |
| **TAINT** | TAINT | **MAYBE** | MAYBE | TAINT |

关键区分：表达式**内部**的数据依赖合并用的是 `combine` 而非 `join`——
`x + "字面量"`、`concat("a", x)` 是单条路径上的一次计算，干净常量与污点运算数
结合仍得 TAINT（不存在"另一条干净路径"），不会误降级为 MAYBE。只有**控制流
汇聚**（兄弟分支、循环多轮）的 CLEAN+TAINT 才产生 MAYBE。这保证了：

- 直线/跨函数/递归的确定污染 → `definite`（tainted）；
- 仅在某分支或某次循环污染 → `conditional`（maybe）。

`BOTTOM` 的中性元性质还保证了不动点的正确性：函数在实参尚未就绪的预热轮次不会
凭空产生一条"CLEAN 兄弟路径"，从而避免把后来的真污点错误合并成 MAYBE。

### 5.2 过程内：流敏感

每个函数编译为显式 CFG（`SIMPLE/BRANCH/LOOP/JOIN/EXIT`），抽象环境挂在
**边**上。`return` 直接连到函数唯一 `EXIT`，不会把返回值漏到调用点后继语句。
`while` 的循环头区分**首次输入（仅前向边）**与**迭代输入（join 回边）**，
从而 `while(false){ x=input(); }` 不会污染退出后的 `x`。

### 5.3 过程间：调用串上下文敏感

- 用长度上界 `K`（默认 3）的函数名调用串区分上下文，例如
  `<entry> > main > build > render`。不同调用方不会互相污染。
- 同一被调函数在不同上下文里克隆一份参数环境/返回摘要；调用点在读取返回值时，
  只接受**该调用点实参**能解释的证据链（参数链按调用点重基），因此
  `f(input())` 的污点不会泄漏到 `f("literal")` 的结果。
- 递归串重复时复用同一上下文；调用串超长时**合并**（join）而不是丢弃污点，
  合并只会损失精度（可能误报），不会漏报。

### 5.4 不动点与终止性

环境、参数绑定、返回值都只在有限格上单调上升；证据链设有数量上界
（`CHAIN_CAP=8`）与长度上界（`HOP_CAP=24`），返回摘要会规范化掉调用方专属的
`call-ret` 跳。因此状态空间有限，工作流算法必然到达不动点。响应中的
`fixpoint_iterations` 即实际迭代次数。

### 5.5 条件净化（守卫精化）

只承认两类**语义可靠**的净化：
1. 数据清洗：`y = escape(x)` 后 `y` 干净；
2. 校验型守卫：`if (validate(x)) { ... }` 的真分支上 `x` 干净（支持
   `and/or/not` 的布尔组合）。

**有意不做**：`if (x == "admin")` 这类与字面量比较**不**净化 `x`——污点是
来源属性，攻击者完全可能提交同值字符串。识别不出的守卫只产生误报，不漏报。

## 6. 上下文敏感程度与误报边界（重要）

**做到了**

- 流敏感：变量在不同程序点、不同分支/循环轮次可有不同污点级别。
- 有界调用串上下文敏感：区分不同调用串；同一调用点按实参链过滤返回污点。
- 跨函数/递归/相互递归的 source→sink 传播；条件净化；常量不可达分支消除。

**刻意不做 / 已知边界（方向均为「宁可误报，不漏报」）**

1. **非路径敏感（除 validator 守卫外）**：不跟踪整数/字符串的具体取值与条件
   约束。因此 `f(3)` 是否真的到达某个递归分支无法判定，可能把不可达的 source
   分支合并进来（误报）。`RECURSION_MIXED` 夹具即此类。
2. **循环次数不敏感**：`while` 中某轮被净化、某轮未净化时，退出值是 MAYBE
   （`loop_conditional.tl`）。
3. **调用串有界 K**：超长递归/深调用链会合并上下文，可能误报并给出 warning；
   不会因此漏报。
4. **无别名/字段/索引敏感**：语言本身无聚合类型；数组/对象类的强更新不在范围。
5. **未定义函数保守处理**：调用既未定义又非内置的函数时，返回值按 TAINT
   处理并在 `warnings` 中提示（`UNKNOWN_FUNCTION` 夹具）。
6. **validator 可信假设**：把 `validate` 当真分支净化，等价于信任该校验器确实
   能保证安全；这是业界污点工具的通用建模，校验器实现本身的缺陷不在分析范围。
7. 未声明变量按干净的 `nil` 处理并告警（脚本语言的宽容语义）。

## 7. 验收：三类夹具与实测结果

下列夹具均在 `tests/` 中自动化断言，且对**安全/危险两个方向**都验证。

| 能力 | 危险样例（应报） | 安全样例（应不报） |
|---|---|---|
| 跨函数传播 | `CROSS_FN_DANGEROUS`：`load_config` 读源→`build_query` 拼接→`query` | `CROSS_FN_SANITIZED`：`escape` 后返回 |
| 条件净化 | `CONDITION_UNSANITIZED_BRANCH`：else 未净化的 `exec` | `CONDITION_SANITIZED`：validate 真分支 |
| 递归 | `RECURSION_DANGEROUS` / `MUTUAL_RECURSION_DANGEROUS`：基准情形读源沿递归返回 | `RECURSION_SAFE`：基准情形返回字面量 |

证据路径示例（`examples/dangerous.tl`，实测，20 次不动点迭代，definite）：

```
load_config@3: source(read_file)
 -> load_config@3: assign(var raw)
 -> load_config@4: return(return from load_config)
 -> main@13: assign(var data)
 -> build_query@8: propagator(concat)
 -> build_query@9: return(return from build_query)
 -> main@14: call-ret(main calls build_query)
 -> main@14: assign(var sql)
 -> main@15: sink(query)
```

### 实测命令与结果（本仓库，Python 3.12.3）

```
$ pip install -r requirements.lock
$ pytest                                  # 45 passed
$ python scripts/analyze_file.py examples/dangerous.tl      # exit=1 vulnerable (definite)
$ python scripts/analyze_file.py examples/safe_sanitized.tl # exit=0 safe
$ python scripts/analyze_file.py examples/conditional.tl    # exit=1（仅 else 分支，definite）
$ python scripts/analyze_file.py examples/recursion.tl      # exit=1（递归证据链，definite）
$ python scripts/analyze_file.py examples/loop_conditional.tl # exit=1 conditional
```

实测各夹具的不动点迭代次数：危险 20、安全 10、条件净化 6、递归 32、
循环条件净化 16（均在毫秒级收敛）。

报告签名实测：`tests/test_api.py` 验证了 (1) 危险报告 Ed25519 验签通过；
(2) 把 `verdict` 从 `vulnerable` 改成 `safe` 后验签失败。

## 8. 未完成项 / 限制

- 无前端界面（按要求仅后端）。
- 未做多态/动态分派；用户函数必须在同一编译单元内静态可见。
- 过程间分析为单模块、全局函数名空间；无 import/模块系统。
- 报告的证据链在极端程序上可能显示 `truncated: true`（链/上下文上界触发），
  此时仍给出正确的二值结论，只是路径展示被截断。
- 未实现污点清洗后重新污染的"二次源"特殊语法（普通赋值已支持）。
- 密钥默认每进程生成；生产部署请用环境变量 `TAINT_SIGNING_KEY`
  （+ `TAINT_SIGNING_KEY_PASSWORD`）注入固定 Ed25519 私钥。
```
