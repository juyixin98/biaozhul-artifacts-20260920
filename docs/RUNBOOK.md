# 运行记录（RUNBOOK）

本文件如实记录在交付环境中实际执行过的命令与结果。

* 环境：**Linux 6.8.0-90-generic，Python 3.12.3**
* 依赖：仅 Python 标准库，未安装任何第三方包
* 记录时间：2026-09-23

## 1. 自动化测试

命令：

```bash
python3 -m unittest discover -s tests
```

结果（最终一次完整运行）：

```
Ran 91 tests in 5.573s

OK
```

91 个用例全部通过，分布：

| 测试文件 | 覆盖内容 |
| --- | --- |
| `tests/test_lexer.py` | token 种类、优先级贪婪匹配、注释（含嵌套块注释）、字面量转义、行列/偏移位置、词法错误位置 |
| `tests/test_parser.py` | 全部语句与表达式、优先级、两种资源语句写法、重复形参/重复函数、错误位置、常量折叠 |
| `tests/test_cfg.py` | 节点/边种类、if true/false、while true/false/back、throw→catch / →uncaught 路由、嵌套 try 内层优先、位置保留 |
| `tests/test_analyzer.py` | 三类验收场景 + 六种缺陷 + 形参所有权 + 合法重获取 + 常量剪枝 + `while(true)` 不发散 + 展开界可调 |
| `tests/test_reproducibility.py` | 重复运行字节一致、轨迹稳定、路径 id 连续、状态迁移闭合表、重放 state_changes 还原 final_states、多函数 |
| `tests/test_service.py` | /health、/version、成功分析、泄漏诊断、400（语法错误/缺 source/非法 JSON/类型错误/非法选项）、404、loop_bound 生效 |

开发过程中出现并已修复的问题（保留以说明“未通过项→修复”轨迹）：

1. `throw` 词法归类与标点 token kind 规范（`(`→`LPAREN` 等）导致解析
   失败——已统一词法/解析的 kind 约定，全部测试通过。
2. `while(true)` 单分支路径上循环计数只在“分叉”时递增，常量真循环
   触发 `RecursionError`——已把每条边的计数/决策更新统一到
   `_continue`，并由 `test_while_true_does_not_diverge` 回归覆盖。
3. 截断路径决策轮次与上一轮重复——已修正为“未能进入的那一轮”，并由
   `test_decision_iterations_recorded` 断言 `entered=[1,2]`、
   `truncated marker=[3]`。
4. CFG 构建器一处把 `self.node()` 返回的节点 id 当节点对象使用导致
   `AttributeError`——已修正并由全量用例覆盖。

## 2. 命令行分析各示例

命令模板：

```bash
python3 -m resflow.cli analyze <file.rf>
```

实际结果摘要（路径数 / 诊断）：

| 示例 | 路径统计 | 诊断 |
| --- | --- | --- |
| `clean.rf` | 2 completed / 0 truncated | 无（干净基线） |
| `double_release.rf` | 2 completed | `double_release`(true 路径)、`use_after_release`(两路径) |
| `exceptional_exit.rf` | 1 exit + 1 uncaught | `resource_leak db`（仅 uncaught 路径） |
| `partial_init.rf` | 2 exit | true：`use_after_release r`；false：`use_unacquired r` + `resource_leak a` |
| `loop_acquire.rf`（默认 bound=2） | 3 completed + 1 truncated | `double_release`（多轮退出）、`release_unacquired`（0 轮退出） |
| `try_catch.rf` | 2 exit + 1 uncaught | `resource_leak a`（仅 catch 内 rethrow 路径） |
| `infinite_loop.rf` | 0 completed + 1 truncated | 无（验证不发散） |
| `comprehensive_demo.rf` | 9 exit + 3 uncaught + 1 truncated | rethrow 路径泄漏 socket+handle；buffer 部分初始化在 3 条 exit 路径泄漏 |

循环示例（默认 bound）的实际路径决策：

```
path 0: loop_truncated  decisions=true#1, true#2, true#3(truncated)  conn=released
path 1: completed exit  decisions=true#1, true#2, false#2           conn=released
path 2: completed exit  decisions=true#1, false#1                   conn=released
path 3: completed exit  decisions=false#0                           conn=released
```

可复现性实测：

```bash
python3 -m resflow.cli analyze examples/loop_acquire.rf --indent 0 > /tmp/l1.json
python3 -m resflow.cli analyze examples/loop_acquire.rf --indent 0 > /tmp/l2.json
cmp /tmp/l1.json /tmp/l2.json && echo "REPRODUCIBLE: two runs byte-identical"
# -> REPRODUCIBLE: two runs byte-identical
```

异常退出示例的实际路径与状态变化：

```
path 0 terminal=uncaught  decisions=[if:6 -> true]
  db: unacquired --acquire--> held --use--> held   (残留 held => 泄漏)
path 1 terminal=exit       decisions=[if:6 -> false]
  db: unacquired --acquire--> held --use--> held --release--> released
```

错误退出码实测：

* 语法/词法错误：CLI 以退出码 **1** 输出带位置的 JSON 错误。
  例如对含 `@@` 的源码返回 `LexError: unexpected character '@'`，
  位置 `line 2, column 11, offset 19`。

## 3. JSON 服务端到端

启动：

```bash
python3 -m resflow.cli serve --port 8080
# -> ResFlow analysis service listening on http://127.0.0.1:8080
```

实际 `curl` 结果：

```
GET  /health   -> 200 {"status": "ok"}
GET  /version  -> 200 {"language": "ResFlow", "language_version": "1.0.0",
                       "toolchain_version": "1.0.0"}
POST /analyze (analyze_exceptional.json)
   -> 200 path_count=2, resource_leak db paths[0] terminals['uncaught']
POST /analyze (analyze_partial_init.json)
   -> 200 path_count=2, use_after_release / use_unacquired / resource_leak
POST /analyze (analyze_loop.json, loop_bound=3)
   -> 200 path_count=5 (4 completed + 1 truncated)
POST /analyze (analyze_try_catch.json)
   -> 200 path_count=3, resource_leak a paths[0] terminals['uncaught']
POST /analyze (analyze_clean.json)
   -> 200 path_count=2, diagnostic_count=0
POST /analyze (analyze_syntax_error.json)
   -> 400 ParseError "expected ')', got 'return'" location L4:3
```

错误处理边界实测：

```
非法 JSON body ............ 400 invalid_json
缺少 source 字段 .......... 400 missing_source
source 不是字符串 ......... 400 invalid_source
loop_bound 越界(-1/99999) . 400 invalid_option（允许区间 [0, 10000]）
未知路由 /nope ............ 404
```

一键复现：

```bash
python3 -m resflow.cli serve --port 8080 &
bash examples/requests/send_all.sh
```

## 4. 未通过项 / 限制（如实说明）

* 当前无“未通过”的测试用例：91/91 通过。
* 工具层面的有意限制见 README “已知边界”：无跨过程分析、普通变量条件
  只做字面量常量折叠（否则保守地两边都探索）、循环为有界展开
  （`loop_truncated` 显式标注）、无别名分析。
* 不提供前端，仅交付 CLI、HTTP JSON 服务与库 API。
