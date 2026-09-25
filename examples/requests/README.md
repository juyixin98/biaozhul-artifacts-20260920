# 请求样例

每个 `.json` 都是 `POST /analyze` 的请求体（可直接 `curl --data @file`）。

| 文件 | 场景 |
| --- | --- |
| `analyze_exceptional.json` | 异常退出：throw 路径泄漏 |
| `analyze_partial_init.json` | 分支部分初始化 |
| `analyze_loop.json` | 循环获取（`loop_bound=3`） |
| `analyze_try_catch.json` | try/catch 恢复与 rethrow |
| `analyze_clean.json` | 干净基线，零诊断 |
| `analyze_syntax_error.json` | 语法错误，期望 HTTP 400 |

一键发送（先启动服务）：

```bash
python3 -m resflow.cli serve --port 8080
bash examples/requests/send_all.sh
```
