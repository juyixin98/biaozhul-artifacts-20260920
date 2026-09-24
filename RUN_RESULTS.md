# 实际运行记录（RUN_RESULTS）

- 日期：2026-09-24
- 机器：Linux 6.8.0-90-generic，Python 3.12.3
- 依赖：`.venv` 虚拟环境，按 `requirements.txt` 锁定安装（fastapi 0.141.1 /
  uvicorn 0.53.0 / cryptography 50.0.1 / pydantic 2.13.5 / pytest 9.1.1 /
  httpx 0.28.1，全量版本见 requirements.txt）

## 1. 自动化测试

命令：`.venv/bin/python -m pytest -v`

结果：**25 passed, 1 warning in 0.45s**

```
tests/test_analyzer.py::test_lexer_basic_tokens                                   PASSED
tests/test_analyzer.py::test_parser_rejects_redefined_function                    PASSED
tests/test_analyzer.py::test_parser_rejects_undefined_call                        PASSED
tests/test_analyzer.py::test_parser_rejects_wrong_arity                           PASSED
tests/test_analyzer.py::test_undeclared_variable_read_is_error                    PASSED
tests/test_analyzer.py::test_direct_source_to_sink_is_vulnerable                  PASSED
tests/test_analyzer.py::test_clean_constant_is_safe                               PASSED
tests/test_analyzer.py::test_sanitize_kills_taint                                 PASSED
tests/test_analyzer.py::test_taint_propagates_through_binary_ops                  PASSED
tests/test_analyzer.py::test_partial_branch_sanitization_is_reported_may_analysis PASSED
tests/test_analyzer.py::test_all_branch_arms_sanitized_is_safe                    PASSED
tests/test_analyzer.py::test_loop_carried_taint_requires_fixpoint                 PASSED
tests/test_analyzer.py::test_cross_function_propagation_fixture                    PASSED
tests/test_analyzer.py::test_source_inside_callee_reaches_top_level_sink          PASSED
tests/test_analyzer.py::test_safe_fixture_has_no_findings                         PASSED
tests/test_analyzer.py::test_return_only_known_clean_is_safe                      PASSED
tests/test_analyzer.py::test_same_function_called_clean_and_dirty                 PASSED
tests/test_analyzer.py::test_mutual_recursion_converges_and_reports               PASSED
tests/test_analyzer.py::test_direct_recursion_converges                           PASSED
tests/test_analyzer.py::test_recursion_with_clean_argument_is_safe                PASSED
tests/test_analyzer.py::test_health_endpoint                                       PASSED
tests/test_analyzer.py::test_analyze_endpoint_dangerous_signed                    PASSED
tests/test_analyzer.py::test_analyze_endpoint_safe                                PASSED
tests/test_analyzer.py::test_analyze_endpoint_parse_error                         PASSED
tests/test_analyzer.py::test_verify_signature_endpoint                            PASSED
======================== 25 passed, 1 warning in 0.45s =========================
```

唯一 warning 来自 starlette TestClient 对 httpx 的弃用提示，与本项目代码无关。

## 2. HTTP 服务 + 全部夹具实跑（真实 uvicorn，非 mock）

启动：`TAINT_KEY_DIR=.data .venv/bin/uvicorn app.web:app --host 127.0.0.1 --port 8000`

`GET /health` → `{"status":"ok","service":"taint-analysis","version":"1.0.0"}`

7 个夹具经 `POST /analyze` 的真实结果：

| 夹具 | vulnerable | findings | SCC 不动点轮数 | 结论 |
|---|---|---|---|---|
| cross_function.tl | true | 1 | 0 | 跨 2 函数（实参+返回值），13 步证据路径 |
| source_in_callee.tl | true | 1 | 0 | source 在被调函数、sink 在顶层 |
| loop_fixpoint.tl | true | 1 | 0（函数内 worklist 不动点） | 循环第 4 轮才出现污点 |
| recursion.tl | true | 1 | **3** | 最短路径必须穿过 unroll↔step 递归环 |
| conditional_sanitize_fp.tl | true | 1 | 0 | 单臂净化 → may 分析报告（误报边界） |
| conditional_sanitize_safe.tl | false | 0 | 0 | 两臂均净化 |
| safe.tl | false | 0 | 0 | 常量，无 source |

危险/安全样例均符合预期。

## 3. 递归夹具的证据路径（examples/client.py 输出，签名验证 VALID）

```
file:       tests/fixtures/recursion.tl
vulnerable: True
findings:   1
signature:  VALID (Ed25519)
  - source <main>@22:5 -> sink step@16:9
    call chain: unroll -> step -> step::sink  (7 steps)
      [  22:5] source  <main>   source() produces attacker-controlled data
      [  22:1] assign  <main>   assigned to variable 'v'
      [  23:1] call    <main>   calls unroll(3, v)
      [   7:1] param   unroll   parameter 'data' enters function unroll
      [  9:16] call    unroll   calls step(n, data)
      [  14:1] param   step     parameter 'data' enters function step
      [  16:9] sink    step     tainted value reaches sink(data)
```

跨函数夹具 client 输出同样签名 VALID，证据链 13 步：
source(main) → forward → sanitize_like → return 链 → sink(main)。

## 4. 签名/验签

- `GET /public-key` 返回 Ed25519 PEM 公钥。
- 每个 `/analyze` 响应的 `signature` 用服务私钥对 `report` 的规范 JSON
  （`sort_keys=True, separators=(",",":")`）签名；`examples/client.py`
  独立用 cryptography 验签通过。
- 自动化测试中：篡改 `report.vulnerable` 后验签失败；`/verify-signature`
  对正确签名返回 `valid:true`，对伪造签名 `AAAA` 返回 `valid:false`。

## 5. 错误处理实测

- `x = ;` → `{"ok":false,"error":{"type":"ParseError",
  "message":"unexpected token ';' at line 1, col 5"}}`
- 调用未定义函数 / 实参数目不符 / 读取未声明变量 → `AnalysisError`（带行列号，
  各有测试覆盖）。

## 6. 额外手工检查（非测试套件）

- sink 出现在 `while` 条件表达式的被调函数中：可正确检出。
- 不可达函数内部的 source→sink：不报告（只统计入口可达路径）。

## 7. 未完成 / 已知限制

- 未做路径敏感分析与常量条件折叠（单臂净化误报是有意保留并明确文档化的边界）。
- 不跟踪隐式流（控制依赖）。
- 无数组/结构体/全局/闭包的建模（语言刻意不包含这些特性）。
- 无鉴权、无限流；签名只保证报告完整性与来源，不提供机密性。
- 未提供 Dockerfile/CI 配置（按“纯后端 + 自动化测试”范围交付，本地 venv 即可复现）。
