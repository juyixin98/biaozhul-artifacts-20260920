# 运行记录（RUN_RECORD）

本文件如实记录本项目在交付环境中的实际运行情况。环境：
Linux 6.8.0-90-generic，Python 3.12.3（仅标准库，无第三方依赖）。

## 1. 自动化测试

命令（在项目根目录）：

```bash
python3 -m unittest discover -s tests -t .
# 或
python3 tests/run_tests.py
```

最近一次结果：**39 个测试全部通过，exit=0**

```
Ran 39 tests in 0.554s
OK
```

分模块：

| 模块 | 用例数 | 结果 | 覆盖内容 |
|---|---|---|---|
| tests/test_frontend.py | 15 | OK | 词法错误与列号、未闭合字符串、注释/行号、缺分号、空程序、else-if 脱糖、重名函数、未声明调用、实参数量、未定义变量、受检异常（throw/调用未捕获必须 throws）、try 内 throw 合法、catch 内再抛、条件中禁调用 |
| tests/test_analyzer.py | 16 | OK | 干净程序、分支部分初始化、重复释放+释放后使用、循环有界展开、循环泄漏/覆盖、异常边进入 catch 清理、异常退出泄漏、嵌套 try 再抛、may_return/may_throw 摘要、必抛函数无虚假正常边、条件抛错两结局、无限递归 divergent、跨运行字节级可复现、路径含状态与 span、CFG 异常边形状 |
| tests/test_server.py | 8 | OK | /health、/analyze 200、发现缺陷、E-PARSE→400 且带 span、缺 source→400、非法 JSON→400、loop_bound 选项、未知路由 404 |

未通过项：**最终交付版本无未通过项**。开发过程中发现并已修复的问题见第 4 节。

## 2. CLI 实际运行

命令与退出码（`python3 -m resflow.cli check <file>`）：

| 示例 | exit | 路径数 | 发现 |
|---|---|---|---|
| examples/01_clean.rf | 0 | 2（fail 1 异常 + main 1 正常） | 无 |
| examples/02_partial_init.rf | 0 | 2 | USE_NOT_HELD, RELEASE_NOT_HELD（警告） |
| examples/03_double_release_and_uaf.rf | 1 | 3 | DOUBLE_RELEASE, USE_AFTER_RELEASE |
| examples/04_loop_acquire.rf | 0 | 4（0/1/2 次迭代 + 1 条 divergent 截断） | 无 |
| examples/05_loop_reacquire_leak.rf | 1 | 4 | RESOURCE_LEAK ×2, OVERWRITE_HELD |
| examples/06_exception_cleanup_ok.rf | 0 | 2（fail 必抛 + main 仅经 catch 一条） | 无 |
| examples/07_exception_exit_leak.rf | 1 | 3 | RESOURCE_LEAK（异常出口） |
| examples/08_nested_try_rethrow.rf | 0 | 3（三个函数各 1 条） | 无 |

退出码约定：0 无 error 级发现（警告不影响）；1 有 error 级发现；
2 词法/语法/语义错误或文件读取失败。

注意：02 的警告类发现 exit 仍为 0；05 在 loop_bound=2 下产生两条
RESOURCE_LEAK（一条“循环一次后出口仍持有”，一条“回边后对 HELD 变量
再次 acquire”），与 README 第 6 节描述一致。

## 3. HTTP 服务实际运行

启动：`python3 -m resflow.cli serve --port 8091 --quiet`

实测（真实抓取）：

```
GET  /health  -> 200 {"status":"ok","language":"resflow/1"}
POST /analyze (request_partial_init.json)
     -> 200 counts={'functions':1,'paths':2,'findings':2,'truncated_paths':0}
POST /analyze (request_double_release.json)
     -> 200 findings: #1 USE_AFTER_RELEASE 10:9, #2 DOUBLE_RELEASE 7:9
        main 路径: path2 exception findings[1]; path3 normal findings[2]
POST /analyze (request_exception_exit.json)
     -> 200 路径 [(1,exception,[]),(2,normal,[]),(3,exception,[1])]
POST /analyze (request_bad_syntax.json) -> 400
     {"error":{"code":"E-PARSE","message":"expected ';' but found '}'",
               "span":{... "line":1,"column":35 ...}}}
```

`samples/curl_examples.sh` 可一键复现（先启动服务，默认 127.0.0.1:8080）。
`samples/output/` 保存了一次真实运行的响应快照（health、三个分析结果、
一个 400 错误）。

## 4. 开发过程中发现并修复的问题（如实记录）

1. **异常边被后继 try 误吞**：第一版 CFG 用“悬挂异常端点 + 后继链”
   传递异常，外层语句的入异常会错误地被后面的 try/catch 捕获。
   修复：改为在创建 throw/call 节点时，按词法最近 try（构建期 ctx 栈）
   **直接**把 exception 边接到对应 catch 头或函数 except_exit。
   test_catch_routes_exception_edges、test_nested_try_rethrow 覆盖。
2. **函数末尾落入 exit 未做泄漏检查**：只在 return 节点检查。
   修复：统一在合成 `exit`（正常）与 `except_exit`（异常）做泄漏检查。
3. **必抛函数产生虚假“正常返回”路径**：`f(){throw}` 被调用后，调用点
   之后的死代码仍出现在正常路径上。修复：新增跨函数结局摘要
   （may_return / may_throw 调用图最小不动点，summaries.py），枚举时
   剪枝不可能结局。test_always_throwing_callee_has_no_spurious_normal_path
   等 4 个用例覆盖。
4. **受检异常未强制**：最初非 throws 函数里可以直接 throw。修复：语义
   检查加入规则——throw/调用 throws 函数必须在 throws 函数内或处于
   try 体；catch 体内抛出不受本 try 捕获。相应把示例 03/07 的 main
   标注为 `throws`。
5. **管道下游提前关闭导致 BrokenPipeError**：`... | head` 时 CLI traceback。
   修复：main 捕获 BrokenPipeError，按惯例返回 0。
6. 若干测试期望按更精确行为修正（列号 20→22；catch 清理示例从“两条
   正常路径”修正为“必抛后仅经 catch 的一条”），均为测试断言问题，
   非分析器缺陷。

## 5. 已知局限（设计使然，非缺陷）

* 路径有界枚举，分支/循环界外行为用 `divergent` 终态显式标注；
  不声称分析过界外。
* 不求值条件（两个分支都探索），不做常量条件剪枝。
* 资源按本地变量标识，不做跨过程别名/所有权转移。
