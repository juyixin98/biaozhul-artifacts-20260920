# 测试报告

- 运行环境：Node.js v22.23.3 / npm 10.9.9 / Linux x64，依赖按 `package-lock.json` 锁定安装。
- 测试框架：Vitest 3.2.7（v8 覆盖率）。无外部服务、无真实账号；SQLite 使用 `:memory:`。
- 最终结果：**5 个测试文件 / 44 个测试全部通过，0 失败、0 跳过。**

```
 ✓ test/unit/parser.test.ts (8 tests)
 ✓ test/unit/state-redact.test.ts (8 tests)
 ✓ test/unit/operations.test.ts (9 tests)
 ✓ test/unit/kernel.test.ts (11 tests)
 ✓ test/integration/http.test.ts (8 tests)

 Test Files  5 passed (5)
      Tests  44 passed (44)
```

覆盖率（v8，门槛 80%）：

```
All files        |  89.27% stmts | 87.02% branch | 82.75% funcs | 89.27% lines
 src/protocol    | 100% / 98.03% / 100% / 100%
 src/state       | 95.77% / 88.23% / 91.3% / 95.77%
 src/kernel      | 90.95% / 85.71% / 88.23% / 90.95%
 src/diagnostics | 76.22% / 71.42% / 50% / 76.22%
 src/transport   | 77.68% / 85% / 75% / 77.68%
```

诊断与传输模块的语句覆盖率低于整体但**总体门槛全部通过**；未覆盖的主要是
stdout JSON 日志的各分支与 Fastify 错误/边界路径（核心裁决逻辑由内核测试覆盖）。

## 测试如何对应需求点

| 需求 | 覆盖测试（文件 → 用例） | 断言内容 |
| --- | --- | --- |
| 通知无响应但记录执行失败 | `kernel.test.ts` → “records a notification failure…”；`operations.test.ts` → async 通知失败 | 204/无响应体；台账 call 行 `status=error`、`errorCategory=business_rule`、`errorCode=-32001`；异步通知操作 `failed/-32020` |
| 空批次与单对象区分 | `parser.test.ts` → “distinguishes an empty batch from a single object”；`kernel.test.ts` → 空批次/坏 JSON | `[]` → `emptyBatch/-32600/400`；单对象 → `single`；全通知批次 → 204 空体 |
| 重复 ID 不串结果 | `kernel.test.ts` → “duplicate RPC ids without cross-talk”、“numeric 1 / string \"1\" / null” | 三个同 id 响应结果分别为 3 / 300 / echo；台账三行独立 callCorr、位置 0/1/2；1、"1"、null 不塌缩 |
| 解析错误与业务错误分层 | `parser.test.ts`；`kernel.test.ts` → 分层用例 | -32700 parse_error（layer=parse，500）；-32600 invalid_request（layer=contract，400）；-32602 invalid_params 与 -32001/-32002 business_rule 分离 |
| 独立操作号、RPC ID 不当幂等键 | `operations.test.ts` → “fresh opSeq per invocation even with same RPC id” | 同一 RPC id 两次调用得到 opSeq 1、2，副作用都发生 |
| 显式幂等键 | `operations.test.ts` → replay / 失败可重试 / 并发串行化 | 第二次 `replayed=true` 且复用 opSeq，余额只变动一次；失败不毒化重试；并发两个请求恰好一次副作用 |
| 异步执行 | `operations.test.ts` → “accepts immediately…“ | 立即 pending+opSeq；后台完成后 operations.get 为 succeeded |
| 乱序完成 | `kernel.test.ts` → “REQUEST order even though execution completes out of order” | 测试侧独立观察者记录完成序为 math.add→sleep，但响应数组仍是 slow 在前、fast 在后 |
| 混合通知/非法批元素 | `kernel.test.ts`、`http.test.ts` → mixed batch | 通知被省略；非法元素单独 -32600 且 id=null；method_not_found=-32601 |
| 真实连接中断 | `http.test.ts` → raw `node:net` 套接字用例 | 请求 undecidable/interrupted；慢调用与操作 interrupted，快调用 success；1.1s 后晚到续体不覆盖终态；诊断接口给出 undecidable 解释 |
| 脱敏 | `state-redact.test.ts`、`operations.test.ts` → redaction | 台账/日志只含 `***redacted***`，不含明文 secret；返回值也不含明文 |
| 诊断带标识与理由 | `http.test.ts` → diagnostics 用例 | 错误 correlationId 可在 `/diag/requests/:corr` 查到 verdict/layer/category/rationale |

测试期望值全部为测试文件内的字面量，不调用被测实现生成“标准答案”；
乱序用例的“真实完成顺序”由测试侧包装方法独立观测。

## 开发过程中出现并已修复的失败（按要求保留）

1. **正常请求被误判中断（真实缺陷）**：初版在 Fastify 中监听 `req.raw`（IncomingMessage）的
   `close` 事件。Node 中该事件在请求体读取完毕时即触发，并非客户端断连，导致冒烟时
   单请求/批次返回空、台账出现大量 `undecidable`。修复：改监听 **socket** 的 `close`，
   并以 `reply.raw.writableFinished` 守卫。修复后冒烟与全部测试正常。
2. **中断后回送 HTTP 499**：语义上客户端已断开，回状态码会让 fetch 得到一个“响应”。
   修复：`reply.raw.destroy()` 销毁套接字；集成测试改用原始 `node:net` 套接字，
   避免 undici 自身的 unhandled socket rejection 噪音。
3. **两处测试自身期望错误（非实现缺陷）**：
   - 转账第二个用例把“来源账户 acc-2 余额 50,050”误写成 99,950（方向写反），已改为断言
     `fromBalance=50050 / toBalance=99950`；
   - 幂等并发用例的负载漏写 `id`，实际是通知（返回 `notificationOnly`），已补 `id`。
4. **request 台账行缺少字段映射**：`getRequest/listRecentRequests` 直接返回 snake_case 行，
   诊断层读不到 camelCase 字段；已补 `mapRequest`。
5. 模块整理：`serializeOperation` 初版放在 kernel 中，diagnostics 反向依赖内核；
   已移至 `src/state/serialize.ts`。

## 未执行 / 未覆盖项

- 无跳过（skip/todo）的测试，无已知未修复失败。
- 已知非目标范围：鉴权/多租户、跨进程/多实例的幂等（当前幂等锁为单进程内）、
  HTTP 压缩与 Keep-Alive 调优、Windows 平台验证。`node:sqlite` 在 Node 22 仍标记
  Experimental（启动以 `--disable-warning=ExperimentalWarning` 抑制警告）。
