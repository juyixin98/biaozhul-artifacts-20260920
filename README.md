# Storage Layout Compatibility Checker

面向 Solidity 编译器 `storageLayout` JSON 输出的**离线存储布局兼容检查器**。
纯后端，TypeScript 实现，既可作为库调用，也提供 Fastify HTTP 服务与 CLI。

它按 `slot` / `offset` 与类型定义**递归**比较两个版本的合约存储布局，
识别可升级合约开发中常见的破坏性/安全变化，并输出**最短差异路径**与
**来自编译器输出的原始证据**。

> 设计原则：**无法判定绝不默认安全**。遇到不认识的类型编码/缺失的类型定义，
> 结论为 `unknown` 而不是 `compatible`。

---

## 1. 判定结论（verdict）

| verdict        | 含义                                                                 |
| -------------- | -------------------------------------------------------------------- |
| `compatible`   | 无错误、无警告；存在的 `info` 均为被规则明确允许的变化（尾部追加等） |
| `unknown`      | 出现未知类型 / gap 储备耗尽 / gap 被删除等需人工确认的情况            |
| `incompatible` | 存在明确破坏存储兼容性的错误（字段移动、插入、类型变化等）             |

判定优先级：`incompatible`（任一 error）> `unknown`（任一 warning）> `compatible`。

## 2. 兼容规则摘要

**明确允许（报 `info`，仍判 `compatible`）：**

1. **根区域尾部追加**新变量（新 slot，或同一 slot 内打包剩余空间）。
2. **`__gap` 原地缩小**，释放区间精确承接新变量（OpenZeppelin 模式）；
   gap 同名缩到长度 0 也允许（0 长数组不占存储）。
3. **结构体尾部追加**成员——但仅当该结构体本身位于父区域末端
   （增长不会推移任何既有成员）；同一 slot 内的打包追加同样允许。
4. **穿过 `mapping` / 变长数组之后**，其内部结构体允许尾部追加：
   这些存储由 `keccak(key, slot)` 独立寻址，彼此不共享线性槽位。
5. 仅类型/结构体**重命名**但结构一致（`renamed`）。
6. 尾部新增 gap 储备（`gap-extended-trailing`）。

**明确破坏（报 `error`，判 `incompatible`）：**

- 已有字段/结构体成员被删除、位置（slot/offset）移动；
- 新字段插入到已有存储区中间（含打包 slot 内部插入）；
- 基本类型位宽变化（`uint128→uint256`）、同宽不同种类（`bytes32→uint256`）；
- 定长数组长度变化、数组元素类型变化；
- `mapping` **键**类型变化（槽位派生完全改变）；值类型递归比较；
- enum 底层宽度变化（成员数跨过 8/16 位边界）；
- gap 发生移动或扩张；
- 继承线性化变化：中间插入新基类把后继变量整体挤位、基类顺序重排。

**保守告警（报 `warning`，判 `unknown`）：**

- `unknown-type`：未知编码（编译器未来版本/自定义）、类型 id 未在 `types` 表定义；
- `gap-overflow`：gap 储备耗尽，新字段越过旧区域末端——本合约平坦追加可行，
  但若存在后继继承合约将发生碰撞；
- `gap-removed`：gap 数组被整体删除（即使区间恰好被承接，安全标记消失需人工确认）。

## 3. 最短差异路径与证据

每条发现包含：

- `kind` / `severity`；
- `path`：从合约根变量到差异点的最短路径，例如
  `m` → `[value]` → `[]` → `items` → `[]` → `z`
  （`[value]`=mapping 值、`[key]`=mapping 键、`[]`=变长数组元素、`[*]`=定长数组元素）；
- `message`：人类可读说明；
- `oldEvidence` / `newEvidence`：编译器原始字段（`slot`、`offset`、类型 id、
  `numberOfBytes`、`contract` 等）。

## 4. 目录结构

```
src/
  types.ts      输入/输出类型（与 solc storageLayout 字段对齐）
  typeModel.ts  类型分类与解析（struct/mapping/array/enum/elementary/unknown）
  compare.ts    核心比较引擎（区域记账 + 递归类型比较 + 继承分析）
  index.ts      库入口（compareLayouts / check）
  cli.ts        命令行
  server.ts     Fastify 服务
samples/
  generate.ts   样例生成器（solc 同构打包规则）
  pairs/        21 对编译器输出样例（v1/v2 storageLayout JSON + contracts.sol 说明）
tests/          node:test 测试（63 个用例，含 HTTP 端到端）
scripts/
  acceptance.ts 一键验收（tsc + 测试 + 真实 HTTP + CLI 退出码）
```

## 5. 本地启动

要求 Node.js ≥ 18。

```bash
npm install        # 依赖已锁定在 package-lock.json
npm run gen-samples # （可选）重新生成样例
npm test           # 运行测试
npm run build      # 编译到 dist/
npm start          # 直接用 tsx 启动 HTTP 服务（默认 127.0.0.1:8080）
# 或: npm run build && npm run start:dist
HOST=0.0.0.0 PORT=9090 npm start
```

### HTTP API

```bash
curl -s http://127.0.0.1:8080/healthz

curl -s -X POST http://127.0.0.1:8080/check \
  -H 'content-type: application/json' \
  -d "{
    \"old\": $(cat samples/pairs/01-safe-append/v1.storageLayout.json),
    \"new\": $(cat samples/pairs/01-safe-append/v2.storageLayout.json)
  }"
```

- `POST /check`：body `{ old, new, gapNamePattern? }`
  （`new` 也接受别名 `neu` / `newLayout`；`old` 接受 `oldLayout`）。
- `POST /check-artifacts`：body `{ oldArtifact: {storageLayout}, newArtifact: {storageLayout} }`，
  可直接粘贴 solc 标准 JSON 的 artifact 片段。
- `gapNamePattern`：识别 gap 变量名的正则字符串，默认 `^__gap(?:_[A-Za-z0-9]+)*$`。
- 输入不合法返回 `400`；检查成功恒返回 `200`，结论在 body 的 `verdict` 字段。

### CLI

```bash
npx tsx src/cli.ts samples/pairs/04-gap-shrink/v1.storageLayout.json \
                   samples/pairs/04-gap-shrink/v2.storageLayout.json
npx tsx src/cli.ts old.json new.json --json          # 完整 JSON 报告
npx tsx src/cli.ts old.json new.json --gap '^gap$'   # 自定义 gap 名
```

退出码：`0` compatible · `1` incompatible · `2` unknown · `64` 输入错误。

### 作为库使用

```ts
import { compareLayouts, check } from './dist/index.js';

const report = compareLayouts(oldStorageLayout, newStorageLayout);
// report.verdict / report.findings / report.summary

// 或传入带 .storageLayout 的 solc artifact：
check(oldArtifact, newArtifact, { gapNamePattern: /^__gap$/ });
```

## 6. 验收命令

```bash
npm run acceptance
```

该命令依次执行：严格类型检查 → 63 项测试 → **启动真实 HTTP 服务并发真实请求
验证三档结论** → 验证 CLI 三种退出码。任一步失败即以非零码退出。

也可手动快速验证：

```bash
npm test
for p in 01-safe-append 03-packed-insert 18-unknown-type; do
  npx tsx src/cli.ts samples/pairs/$p/v1.storageLayout.json samples/pairs/$p/v2.storageLayout.json \
    >/dev/null; echo "$p exit=$?";   # 期望 0 / 1 / 2
done
```

## 7. 样例对一览

| 样例 | 场景 | 期望结论 |
| --- | --- | --- |
| 01-safe-append | 尾部追加整 slot 变量 | compatible |
| 02-packed-safe | 同 slot 打包追加（uint128×2） | compatible |
| 03-packed-insert | 打包字段中间插入 | incompatible |
| 04-gap-shrink | `__gap[49]→[48]` 承接新变量 | compatible |
| 05-gap-partial-overflow | gap 耗尽且越过旧末端 | unknown |
| 06-gap-exact-boundary | gap 被整体删除（精确承接） | unknown |
| 06b-gap-shrink-to-zero | gap 同名清空为 0 | compatible |
| 07-nested-struct-tail | 末端嵌套结构体打包追加 | compatible |
| 08-nested-struct-mid | 非末端结构体增长推移后继成员 | incompatible |
| 09-mapping-value-struct-append | mapping 值结构体追加 | compatible |
| 10-dynarray-element-append | 变长数组元素结构体追加 | compatible |
| 11-fixed-array-length | 定长数组长度变化 | incompatible |
| 12-mapping-key-change | mapping 键变化 | incompatible |
| 13-width-change | uint128→uint256 | incompatible |
| 14-enum-width | enum 1B→2B | incompatible |
| 15-field-removed | 删除字段 | incompatible |
| 16-inheritance-insert | 中间基类插入 | incompatible |
| 17-gap-in-struct | 结构体内 gap 缩小承接成员 | compatible |
| 18-unknown-type | 未知编码（保守） | unknown |
| 19-type-swap | bytes32→uint256 | incompatible |
| 20-string-bytes-ok | string/bytes 保持不变 | compatible |

样例 JSON 的形状与 solc 标准 JSON 输出中
`contracts[source][name].storageLayout` 完全一致；每个目录下的 `contracts.sol`
给出对应 Solidity 版本说明。

## 8. 输入来源

用 solc 生成 storageLayout（标准 JSON）：

```bash
solc --standard-json --storage-layout input.json
# input.json 的 outputSelection 中选择 "storageLayout"
```

Hardhat / Foundry 产物中的 `storageLayout` 字段也可直接使用
（Foundry: `forge build --storage-layout`，产物在
`out/Contract.sol/Contract.json` 的 `storageLayout`）。
