# 测试说明

## 如何运行

```bash
npm install
npm test              # 81 个用例，失败即非零退出
npm run test:coverage # 同上 + v8 覆盖率门槛（语句/分支/函数/行均 ≥ 80%）
npm run typecheck     # tsc --noEmit（strict + noUncheckedIndexedAccess）
```

## 最近一次实际运行结果（开发机，Node v22.23.3）

- 测试文件：**9 个，全部通过**；用例：**86 passed，0 failed，0 skipped**。
- 覆盖率（`npm run test:coverage`）：

  | 维度 | 覆盖率 | 门槛 |
  |------|--------|------|
  | Statements | 89.0% | 80% |
  | Branches | 82.6% | 80% |
  | Functions | 95.8% | 80% |
  | Lines | 90.9% | 80% |

- 类型检查：`tsc --noEmit` 无错误。
- 另做过真实 HTTP 冒烟（`npm start` + curl，见 `examples.md`）：ping、
  整数溢出（-32602）、空批次（200 + 单 -32600）、全通知批次（204）、
  坏 JSON（-32700 且不回显密钥）、副作用写入、camelCase 密钥静态存储脱敏、
  解析失败诊断不落密钥，均符合预期。

## 测试文件与职责

| 文件 | 关注点 |
|------|--------|
| `tests/parse.test.ts` | -32700 vs -32600 分层；`[]` 与单元素批次；通知 vs `id:null`；非法 id/params |
| `tests/batch.test.ts` | 乱序完成的位置归属；通知无响应但有副作用；204；批次内重复 id -32001；跨批次同 id 不碰撞；数字/字符串 id 区分；非法元素逐条 -32600；批次元素数上限 |
| `tests/idempotency-async.test.ts` | 独立 operationId；拒绝被静默忽略的 idempotencyKey 字段；同键回放不再副作用；同键异参 -32005 审计行；在途 -32005；任务 pending→succeeded/failed(-32050)/cancelled(-32007)；连接断开后分离任务存活；通知启动任务；整数溢出/布尔/越界/未知方法错误码 |
| `tests/sqlite-store.test.ts` | SQLite 持久化重开、(method,key) 唯一约束、NULL 键不碰撞、按方法作用域、主键重复、内存/SQLite 的 events limit 子集一致性 |
| `tests/diagnostics.test.ts` | 脱敏（嵌套/camelCase/Pascal/header/复合词/jwt/privateKey）、密钥不入库、解析错误诊断不落原文密钥、三类 decision 与关联标识、sink 行 |
| `tests/http-integration.test.ts` | 真 TCP：混合批次、204、-32700、空体、通知副作用落库、真实 `AbortController` 中断（分离任务存活 / 附着任务 -32008 且副作用不落库） |
| `tests/kernel-units.test.ts` | id 键/指纹/任务注册表（取消、drain、drain 超时）/严格校验器/脱敏边界 |
| `tests/state-units.test.ts` | 内存领域/状态存储的缺失、重复、过滤、关闭分支 |
| `tests/config.test.ts` | 启动配置边界（端口范围、非整数、延迟非负、日志级别） |

测试期望值均为**手写字面量**（具体错误码、状态、位置、脱敏后值），不是由
被测核心重新生成；内存夹具与 SQLite 双适配分别被测，避免"自己证明自己"。

## 明确未覆盖 / 刻意不做（未执行项）

- **跨进程恢复**未实现也未测试：分离任务的进程内注册表不跨重启恢复。
  崩溃后在途操作在 SQLite 中保持 `pending`（可查询的诚实状态），但不会自动续跑。
  仅有"单进程内 drain / 取消 / 查询"的测试。
- 无鉴权、多租户、TLS、限流测试——本地单用户服务的既定范围。
- 不做真实外部网络/账号/数据库集成（全部本地夹具），因此无对应集成测试。
- 超大批量/1 MiB body 上限有解析器代码与常量，但没有专门的大体量压测。
- 覆盖率不是 100%：`main.ts`（进程信号/启动样板）被排除在门槛外；
  少量防御性分支（罕见对象类型、Fastify 内部分支）未逐一构造输入。
