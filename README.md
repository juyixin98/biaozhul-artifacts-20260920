# storage-layout-compat-checker

离线 Solidity 存储布局兼容检查器。输入两份 solc 编译器输出的 `storageLayout` JSON
（`solc --storage-layout`，或 Foundry/Hardhat 构件中的 `storageLayout` 字段），
按 **slot / offset / 类型** 递归比较，判定升级是否存储兼容。纯后端：既可作为
TypeScript 库调用，也提供 Fastify HTTP 服务。

## 判定规则

**verdict** 三态：

| verdict | 含义 |
|---|---|
| `compatible` | 仅有追加类变更（显式兼容规则命中），无任何冲突 |
| `incompatible` | 存在 error 级发现（字段移动/插入/删除、类型宽度变化等） |
| `unknown` | 存在无法判定的类型（描述缺失或未知 encoding）——**保守，不默认安全** |

**显式兼容规则（info）：**
- `FIELD_APPENDED` — 新状态变量追加在所有旧变量之后（slot ≥ 旧布局末尾）
- `STRUCT_MEMBER_APPENDED` — 结构体新成员追加在所有旧成员之后
- `GAP_REDUCED` — OpenZeppelin 风格 `uint256[N] __gap` 缩减，且缩减量与
  新变量占用的 slot 数精确一致（gap 起点前移、长度同步缩短）

**不兼容（error）：** 字段删除/移动（含继承重排导致的 slot 变化与声明合约变化）、
字段插入、值类型宽度或标签变化、encoding 种类变化、结构体成员删除/移动/中间插入、
静态数组长度变化、数组元素或映射键值类型的不兼容变化、gap 缩减与新变量占用不一致。

**保守未知（warning → verdict `unknown`）：** `types` 表中缺少类型描述、
未识别的 encoding。宁可报“无法判定”，绝不默认安全。

**差异路径与证据：** 每条 finding 带最短差异路径（如 `Token.meta.cap`、
`Token.balances.<value>`、`C.xs[*]`）和 old/new 两侧证据（slot、offset、
类型 id、类型标签、字节宽度）。

## 目录结构

```
src/types.ts    storageLayout 与报告类型定义
src/compare.ts  核心比较引擎（库）
src/index.ts    库入口（compareLayouts）
src/server.ts   Fastify HTTP 服务
scripts/check-samples.ts  运行全部样例对并校验预期结论
samples/        成对编译器输出样例（v1 ↔ v2-append / v2-gap / v2-breaking）
test/           vitest 测试：嵌套结构、打包字段、gap 边界、继承移动、未知类型等
```

## 本地启动

```bash
npm install        # 依赖已锁定（package-lock.json）
npm test           # 运行全部自动化测试
npm run check:samples   # 对 samples/ 下的成对样例给出判定
npm start          # 启动 HTTP 服务（默认 0.0.0.0:3000，PORT/HOST 可覆盖）
# 或编译后运行：npm run build && npm run start:dist
```

## HTTP API

- `GET /health` → `{"status":"ok"}`
- `POST /compare`，body：`{"old": <storageLayout>, "new": <storageLayout>}`
  - 200：`compatible` 或 `unknown` 报告
  - 422：`incompatible` 报告
  - 400：请求体不是合法的 storageLayout 对

示例：

```bash
curl -s -X POST http://127.0.0.1:3000/compare \
  -H 'content-type: application/json' \
  -d "{\"old\":$(cat samples/v1/Token.storage.json),\"new\":$(cat samples/v2-append/Token.storage.json)}"
```

## 库用法

```ts
import { compareLayouts } from './src/index.js';

const report = compareLayouts(oldLayout, newLayout);
// report.verdict: 'compatible' | 'incompatible' | 'unknown'
// report.findings: [{ severity, code, path, message, evidence }]
// 自定义 gap 命名：compareLayouts(a, b, { isGapVariable: e => e.label === '__reserved' })
```

## 验收命令

```bash
npm test                # 28 个测试全部通过
npm run check:samples   # 输出 "all sample pairs match expectations"
npm run build           # tsc 无错误
PORT=3210 npm start &   # 启动后：
curl -s http://127.0.0.1:3210/health                       # {"status":"ok"}
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:3210/compare \
  -H 'content-type: application/json' \
  -d "{\"old\":$(cat samples/v1/Token.storage.json),\"new\":$(cat samples/v2-breaking/Token.storage.json)}"
# -> 422
```

## 样例说明

| 样例对 | 场景 | 预期 |
|---|---|---|
| `v1/Token` ↔ `v2-append/Token` | 末尾追加字段 + 结构体末尾追加成员（结构体为最后一个变量） | compatible |
| `v1/Upgradeable` ↔ `v2-gap/Upgradeable` | `__gap[50]` 中放入两个打包字段，gap 缩减为 `[49]` 并前移一个 slot | compatible |
| `v1/Token` ↔ `v2-breaking/Token` | 继承引入新基类变量导致全体移位 + 结构体成员 `cap` 由 uint128 变 uint64 | incompatible |
