# ResFlow — 资源释放路径分析工具链

一个**纯后端**项目：自定义一门小语言 **ResFlow**（`.rf`），用手写的词法
分析器、递归下降语法分析器、控制流图（CFG）构建器和路径敏感分析器，
对显式 `acquire` / `release` / `return` / `throw` 做控制流分析，检测：

* **重复释放**（double release）
* **重复获取**（double acquire，旧实例泄漏）
* **未释放 / 资源泄漏**（normal exit 与 exceptional exit 都会检查）
* **释放后使用**（use after release）
* **未获取即释放 / 即使用**

异常边是 CFG 上的一等边，和普通边用同一套状态传播参与计算。核心解析与
分析**没有使用任何现成编译器/分析框架**（仅用 Python 标准库做 HTTP 与
JSON）。

完整语言规范见 [`docs/LANGUAGE.md`](docs/LANGUAGE.md)。

## 目录结构

```
resflow/                 工具链库（零三方依赖）
  lexer.py               手写词法分析器（保留行列+字节偏移）
  ast_nodes.py           AST 定义 + 常量折叠
  parser.py              手写递归下降语法分析器
  cfg.py                 手写 CFG 构建（normal/true/false/exception/back 边）
  analyzer.py            路径敏感有界符号执行 + 诊断
  engine.py              source -> JSON 报告的统一入口
  service.py             标准库 http.server 实现的 JSON 服务
  cli.py                 命令行（analyze / serve）
examples/                覆盖三类验收场景的 .rf 程序
examples/requests/       HTTP 请求样例
docs/LANGUAGE.md         语言与分析语义权威文档
tests/                   91 个自动化测试（unittest）
```

## 环境与安装

* Python 3.10+（开发与实测使用 **Python 3.12.3 / Linux**）
* 无需 `pip install`，无第三方依赖。

## 快速开始

### 命令行分析一个文件

```bash
python3 -m resflow.cli analyze examples/exceptional_exit.rf
```

可选参数：

```bash
python3 -m resflow.cli analyze examples/loop_acquire.rf --loop-bound 3 --max-paths 1000
```

### 启动 JSON 服务

```bash
python3 -m resflow.cli serve --host 127.0.0.1 --port 8080
```

接口：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/analyze` | 提交源码做分析 |
| `GET` | `/health` | 存活探针 |
| `GET` | `/version` | 工具链版本 |

`POST /analyze` 请求体：

```json
{
  "source": "fn f(){ acquire(a); release(a); return; }",
  "loop_bound": 2,
  "max_paths": 512
}
```

只有 `source` 必填。词法/语法错误返回 **HTTP 400**，并带精确源码位置。

请求样例（在服务启动后）：

```bash
curl -s -X POST http://127.0.0.1:8080/analyze \
  -H 'Content-Type: application/json' \
  --data @examples/requests/analyze_exceptional.json
```

`examples/requests/` 目录下还有 `.sh` 脚本与其它场景的请求体。

## 输出 JSON 结构（节选）

```jsonc
{
  "language": "ResFlow", "language_version": "1.0.0",
  "options": {"loop_bound": 2, "max_paths": 512},
  "functions": [
    {
      "name": "exceptional",
      "params": [],
      "resource_universe": ["db"],
      "initial_states": {"db": "unacquired"},
      "cfg": { "entry": 0, "exit": 1, "uncaught": 2,
               "nodes": [ ... ], "edges": [ ... ] },
      "paths": [
        {
          "id": 0, "status": "completed", "terminal": "uncaught",
          "node_trace": [0, 3, 4, 5, 6, 7, 2],
          "decisions": [{"node": 6, "edge": "true", "value": true}],
          "state_changes": [
            {"node": 4, "resource": "db", "op": "acquire",
             "before": "unacquired", "after": "held"}
          ],
          "final_states": {"db": "held"},
          "diagnostics": [ ... 路径级诊断 ... ]
        }
      ],
      "diagnostics": [ /* 跨路径聚合的诊断，含 path_ids/terminals */ ],
      "summary": {"path_count": 2, "completed": 2,
                  "loop_truncated": 0, "path_cap": 0,
                  "diagnostic_count": 1}
    }
  ],
  "summary": {"function_count": 1, "path_count": 2, "diagnostic_count": 1}
}
```

每条 CFG 边形如 `{"src":7,"dst":2,"kind":"exception","detail":{"caught":false}}`，
边的 `kind` 取值：`normal / true / false / exception / back`。
每个节点和每条诊断都带 `location: {line, column, offset, end_offset}`。

## 分析方法（简述）

1. **Lexer**：逐字符扫描，产出带源码区间的 token；支持嵌套块注释。
2. **Parser**：递归下降 + 运算符优先级攀爬，产出带位置的 AST。
3. **CFG**：语句逐条下降为节点；`if`/`while` 产生 true/false（及 back）
   边；`throw` 经“待接线帧”栈连到最近 `catch_head` 或函数 `uncaught`；
   `return` 连到函数 `exit`。
4. **Analyzer**：在 CFG 上做确定性 DFS 路径分叉；每路径维护
   `resource → {unacquired, held, released}`；常量条件剪枝；循环按
   `loop_bound` 展开，超限产出可复现的 `loop_truncated` 路径；在两个
   终端节点做泄漏检查。后继顺序固定，因此路径编号与输出可复现。

## 三类验收场景与示例对应

| 验收点 | 示例 | 预期 |
| --- | --- | --- |
| 分支部分初始化 | `examples/partial_init.rf` | true：释放后使用；false：未获取即使用 + a 泄漏 |
| 循环获取 | `examples/loop_acquire.rf` | 展开 2 轮 + 1 条截断路径 + 3 条不同退出路径 |
| 异常退出 | `examples/exceptional_exit.rf` | throw 路径到 `uncaught`，db 泄漏；正常路径干净 |
| 异常恢复 | `examples/try_catch.rf` | catch 补齐释放 / catch 内 rethrow 逃逸泄漏 |
| 重复释放 | `examples/double_release.rf` | double_release + use_after_release |
| 干净基线 | `examples/clean.rf` | 零诊断 |
| 常量死循环 | `examples/infinite_loop.rf` | 仅 1 条 loop_truncated，不发散 |

## 运行测试

```bash
python3 -m unittest discover -s tests -v
```

覆盖：词法（token/位置/注释/错误）、语法（优先级/各语句/错误位置）、
CFG（边种类/异常路由/嵌套 try）、分析器（三类验收场景 + 全部缺陷类型 +
循环边界 + 常量剪枝）、可复现性（重复运行字节一致、状态迁移闭合、重放
state_changes 可还原 final_states）、HTTP 服务（成功/各类 400/边界）。

## 库方式调用

```python
from resflow import analyze

report = analyze(source, loop_bound=2, max_paths=512)
```

## 已知边界（如实说明）

* 函数之间不做跨过程分析（语言里没有调用表达式）；每个函数独立分析，
  形参按“入口即持有所有权”处理，出口必须释放。
* 条件里不跟踪普通变量的取值域，只做**字面量常量折叠**；引用变量的
  条件一律视为不可判定，true/false 两边都探索（这是有意的保守策略，
  保证不漏报）。
* 循环是有界展开，`loop_truncated` 路径明确标注“超出展开界”的状态，
  但不声称覆盖了界之外的无限多轮。
* 不做指针/别名分析：资源名即资源身份，`acquire(a)` 与 `acquire(b)`
  是两个独立资源。
