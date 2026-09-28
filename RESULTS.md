# 验收执行结果

本文件如实记录在**干净环境**中按 README 步骤执行的结果。所有命令均可复现；
下方为实际命令输出摘录（非推算）。

- 执行日期：2026-09-27
- 平台：Linux 6.8.0-90-generic（x86_64）
- 运行时：Node v22.23.3，npm 10.9.9
- 起始基线提交：`69db123 Initial empty baseline`

## 复现步骤与结果

清理生成产物（`dist/ data/ .test-output/`）后，`node_modules` 已重新安装一次，
随后顺序执行：

```bash
npm run typecheck      # 退出码 0
npm run build          # 退出码 0（产物 dist/）
npm run test:coverage  # 退出码 0（含覆盖率门槛校验）
npm run replay         # 退出码 0
npm audit              # 退出码 0（0 vulnerabilities）
```

### 1) 类型检查与编译

```
> tsc -p tsconfig.json --noEmit        typecheck exit=0
> tsc -p tsconfig.build.json           build exit=0
```

编译产物 `dist/` 可用 `node dist/server.js` 直接启动（已用真实 curl 冒烟：
PUT→201、`If-None-Match`→304、`/health`→`{"status":"ok","store":"sqlite"}`）。

### 2) 测试与覆盖率

```
Test Files  10 passed (10)
     Tests  89 passed (89)

All files  | 94.81% Stmts | 86.96% Branch | 97.77% Funcs | 95.36% Lines
Statements 94.81% (457/482)
Branches   86.96% (347/399)
Functions  97.77% (88/90)
Lines      95.36% (432/453)
```

覆盖率门槛在 `vitest.config.ts` 中强制为语句/分支/函数/行均 ≥80%，未达标则命令
退出非零。纯进程装配入口 `src/server.ts` 与纯类型声明 `src/contract/model.ts`
不含分支逻辑，按口径排除；其余源文件全部计测。

测试组成（断言精确状态码、版本、正文、ETag、失败类别，而非“接口能调用”）：

- `test/contract/`：ETag/HTTP-date 单测 15 个；条件求值器对**独立预言机**
  （`test/oracle/`，不 import 任何 `src/` 代码）的 28 个场景交叉校验。
- `test/kernel/`：版本生命周期、404 vs 412 语义、弱标签写、`*` 通配、
  快照同源、幂等重放/409（含跨资源/跨方法作用域冲突）、503 锁忙映射，共 16 个。
- `test/state/`：`worker_threads` 真实磁盘 SQLite 并发 3 个 + 适配器查询/错误映射 2 个。
- `test/http/`：Fastify 端到端 17 个（HEAD、304、412、409、503 分类、诊断接口）
  + 夹具重放 3 个，共 20 个。
- `test/config.test.ts`：配置解析与启动期校验 5 个。

合计 15 + 28 + 16 + 5 + 20 + 5 = 89。

### 3) 手写夹具真实 HTTP 重放

```
[fixture-lost-response] PASS (4 steps)
[fixture-weak-tags]     PASS (6 steps)
[fixture-wildcard]      PASS (6 steps)
ALL FIXTURES PASSED (3)
```

夹具期望 ETag 的哈希由系统外工具独立生成
（`printf '%s' <body> | sha256sum | cut -c1-12`，记录于 `test/fixtures.ts`），
不由被测核心产生。

### 4) 并发与无丢失更新（真实 worker 竞争同一磁盘数据库）

`LOG_TESTS=1 npm run test:coverage` 的关联日志中可逐客户端核对：

```
[concurrency-once/client-A/race]  <-- status=200  verdict=proceed            attempts=[200]
[concurrency-once/client-B/race]  <-- status=412  category=if-match-mismatch attempts=[412]
[concurrency-loop/client-A/race]  <-- status=200  category=if-match-mismatch attempts=[412,200] newVersion=3
[concurrency-loop/client-D/race]  <-- status=200  verdict=proceed            attempts=[200]    newVersion=2
[concurrency-wildcard/creator-A]  <-- status=201  verdict=proceed
[concurrency-wildcard/creator-B]  <-- status=412  category=if-none-match-exists
```

断言结论：

- 两个客户端持同一旧 `If-Match` 同时提交：恰一个 `200`、另一个 `412
  if-match-mismatch`；最终仅 v2 且正文为唯一获胜方，**无丢失更新**。
- 失败方重读新标签后重试：两次写入分别落 v2/v3，都不丢失（历史版本 `[1,2,3]`）。
- 并发 `If-None-Match: *` 创建：恰一个 `201`、另一个 `412 if-none-match-exists`，
  无重复创建。

日志每行带 `runId/clientId/requestId`，并含观察版本、逐步判定
（stage/comparison/observed/result）与结论类别；写入 `.test-output/all.log`，
`LOG_TESTS=1` 时同时输出到控制台。诊断接口
`/diagnostics/decisions?runId=...` 暴露同样的步骤与依据。

## 依赖版本（package-lock.json 实际安装）

| 包 | 版本 |
|---|---|
| fastify | 5.12.5 |
| better-sqlite3 | 13.0.3 |
| typescript | 5.9.3 |
| tsx | 4.23.15 |
| vitest | 5.0.2 |
| @vitest/coverage-v8 | 5.0.2 |
| @types/node | 22.x |
| @types/better-sqlite3 | 7.6.x |

`npm audit`：**found 0 vulnerabilities**。

## 已知边界（如实说明）

- 存储为本地 SQLite（WAL）。高争用下 `busy_timeout`（默认 5000ms）耗尽时
  返回明确的 `503 write-lock-busy`，并在决策日志中留痕，不会静默成功。
- 表示体按文本/字节字符串处理（合成夹具领域足够）；内容类型原样保存与回显。
- 时钟在测试中可注入以获得确定性；服务器默认使用系统时钟。
