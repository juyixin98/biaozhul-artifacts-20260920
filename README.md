# license-propagation — 依赖图许可证策略传播服务（纯后端）

一个基于 **Rust + Axum** 的 HTTP 服务：输入依赖图（每个依赖一条 SPDX
许可证表达式子集 + 每条依赖边的链接方式 static/dynamic）和**项目自定义
允许矩阵**，计算每个包选择哪条许可证择一方案可以满足策略；无解时返回
具体的冲突路径。还提供穷举求解器作为对照预言机。

> ⚠️ **本服务只做规则计算，不提供任何法律结论。**
> 内置的 copyleft 强弱分类表、例外清单、传播默认值均为**说明性占位**，
> 不构成法律意见。所有规则都可以通过请求中的 `policy` 覆盖或扩展。
> 真实合规判断请咨询专业律师。

## 功能

- SPDX 表达式子集解析：`AND` / `OR` / `WITH` 与嵌套括号；关键字大小写不敏感。
  `OR` 展开为“择一”，`AND` 表示同时遵守，表达式自动展开为 DNF。
- 依赖图模型：`from`（消费者）→ `to`（依赖），边带链接方式
  `static` / `dynamic`（默认 static）。
- 项目自定义策略（全部随请求给出，无需配置文件）：
  - `allowed_licenses`：许可证 → 允许的入边上下文
    （`static`、`dynamic`，或根包专用的 `*`）；
  - `compatible_with`：自定义 copyleft 兼容矩阵
    （`GPL-2.0-only: [MIT, ...]`；`"*"` 表示通配）；
  - `copyleft`：覆盖某许可证的强弱分类（`none|weak|strong`）；
  - `exceptions`：追加认可的例外 ID（内置一份说明性清单）；
  - `extra_licenses` / `unknown_licenses`：未知许可证处理；
  - `strong_propagates_on` / `weak_propagates_on`：强弱 copyleft 分别沿
    哪些链接方式传播（默认：强 → static+dynamic，弱 → 仅 static）。
- 约束传播：被选择且带 copyleft 的许可证原子沿**反向**依赖边（依赖 → 消费者）
  传播，传递闭包为不动点（因此天然处理依赖环）；`WITH` 例外只抑制
  **该原子自身**发出的义务，不豁免它兼容外来义务。
- 结果：
  - 可满足 → 每包选择的 term、当前生效的 copyleft 义务；
  - 不可满足 → 允许矩阵冲突（含入边上下文）或一条具体的 copyleft
    冲突传播路径（消费者 → … → 义务来源，含每条边链接方式）；
  - 依赖环（Tarjan SCC + 环上具体路径）**无论是否可满足都会返回**。
- 穷举模式（`exhaustive: true`）：枚举所有可行 term 组合，统计满足
  策略的赋值数量，并与回溯求解器交叉验证（`agrees_with_solver`）。
- 两个求解器实现同一份规则：MRV 回溯 + 单调传播剪枝；以及暴力枚举。
  测试中含 400 个确定性随机小实例逐一对照。

## 依赖

- Rust / Cargo（在 `cargo 1.98.1 / rustc 1.98.1` 上验证）
- 运行时依赖（见 `Cargo.toml` / `Cargo.lock`）：axum 0.8、tokio 1、
  serde 1、serde_json 1。

## 启动

```bash
cargo run --release                 # 默认监听 0.0.0.0:8080
PORT=8080 HOST=127.0.0.1 cargo run  # 自定义地址
```

## 测试

```bash
cargo test                 # 全部 32 个测试（单元 + 集成 + 随机对照）
bash examples/smoke.sh     # 构建、起服务、把 examples/*.json 全部打一遍
```

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET  | `/health` | 健康检查 |
| POST | `/api/validate-expression` | 仅解析/校验 SPDX 表达式，返回 DNF 择一 |
| POST | `/api/analyze` | 完整策略计算 |

错误输入（非法 JSON、未知字段、引用不存在的包、非法表达式等）返回
`400 {"error": "..."}`。

### 请求样例

```bash
curl -s -X POST http://127.0.0.1:8080/api/validate-expression \
  -H 'content-type: application/json' \
  -d '{"expression": "MIT OR (Apache-2.0 AND GPL-2.0-only WITH Classpath-exception-2.0)"}'

curl -s -X POST http://127.0.0.1:8080/api/analyze \
  -H 'content-type: application/json' \
  --data-binary @examples/01_multi_license_choice.json
```

`examples/` 目录：

| 文件 | 场景 |
| --- | --- |
| `01_multi_license_choice.json` | 多许可证择一：`MIT OR GPL-2.0-only` 的 app 静态链接 GPL 库 → 选 GPL |
| `02_conflict_path.json` | app(MIT)→mid(Apache)→lib(GPL) 全静态，返回 app→mid→lib 冲突路径 |
| `03_exception.json` | GPL WITH Classpath 例外不发出义务，MIT 应用可满足 |
| `04_dependency_cycle.json` | plugin↔core 环，MIT 节点无法满足环上 GPL 义务（环 + 冲突） |
| `05_weak_copyleft_dynamic.json` | LGPL 弱 copyleft，动态链接义务不到消费者 |
| `06_custom_matrix.json` | 项目自定义兼容矩阵 `GPL→MIT` 后冲突消失 |
| `07_cycle_satisfiable.json` | 环上两节点都可兼容，环被报告但整体可满足（穷举对照） |

## 请求/响应模型（摘要）

```jsonc
// POST /api/analyze
{
  "packages": [{ "id": "app", "spdx": "MIT OR GPL-2.0-only" }],
  "edges": [{ "from": "app", "to": "lib", "link": "static" }],
  "policy": {
    "allowed_licenses": { "MIT": ["*", "static", "dynamic"] },
    "compatible_with": { "GPL-2.0-only": ["MIT"] },
    "copyleft": { "WeirdLicense": "none" },
    "exceptions": ["MyLinkingException-1.0"],
    "extra_licenses": ["WeirdLicense"],
    "unknown_licenses": "reject",
    "strong_propagates_on": ["static", "dynamic"],
    "weak_propagates_on": ["static"]
  },
  "exhaustive": true,
  "exhaustive_limit": 100000
}
```

```jsonc
// 响应（可满足）
{
  "satisfiable": true,
  "selection": [{ "id": "app", "spdx": "...", "alternatives": [/* DNF, feasible 标记 */],
                  "chosen": { "expression": "GPL-2.0-only", "atoms": [...], "feasible": true } }],
  "cycles": [],
  "active_obligations": [{ "source": "lib", "source_license": "GPL-2.0-only", "strength": "strong" }],
  "exhaustive": { "feasible_assignments": 1, "total_assignments_of_feasible_terms": 2,
                  "capped": false, "cap": 100000, "agrees_with_solver": true },
  "warnings": []
}
```

```jsonc
// 响应（不可满足，冲突路径；环始终单独报告）
{
  "satisfiable": false,
  "selection": [/* 每个 term 的 feasible 标记 */],
  "conflicts": [{
    "kind": "copyleft",
    "source": "lib", "source_license": "GPL-2.0-only", "strength": "strong",
    "path": [
      { "from": "app", "to": "mid", "link": "static" },
      { "from": "mid", "to": "lib", "link": "static" }
    ],
    "target": "app", "target_license": "MIT", "target_has_exception": false,
    "detail": "propagated copyleft obligation ... not listed in the policy compatibility matrix"
  }],
  "cycles": [{ "nodes": ["plugin", "core"], "edges": [/* 闭环边 */] }]
}
```

允许矩阵冲突形如：
```json
{ "kind": "allow", "package": "app", "license": "ISC",
  "context": "*", "detail": "package has no incoming dependency edge (root context '*') ..." }
```

## 规则语义（实现的就是这些规则，不是法律标准）

1. 每包从其 DNF 择一中选一个 term；`AND` term 内所有原子都要成立。
2. 每个被选原子必须：策略已知（`unknown_licenses=reject` 时未知即否决）、
   带例外时例外在认可清单内、许可证在该包入边上下文
   （根包要求条目含 `*`）出现在允许矩阵中。
3. term 内若存在 copyleft 原子，同 term 所有原子必须与其兼容
   （包自身恒在自身义务作用域内，即使该强度不沿任何链接传播）。
4. 被选且无例外的 copyleft 原子按强度沿配置的链接方式反向传播到所有
   传递消费者（包含自身）；路径上每个被选原子都必须在兼容矩阵中。
5. 许可证恒与自身兼容；矩阵未配置某义务时，默认只有同名许可证兼容。

## 代码结构

```
src/spdx.rs    SPDX 子集词法/语法解析 + DNF 展开
src/graph.rs   图、入边上下文、反向可达、最短传播路径、Tarjan SCC 与环提取
src/policy.rs  策略解析、说明性内置表、允许矩阵与兼容矩阵判定
src/solve.rs   预处理、回溯求解器、穷举预言机、冲突/结果渲染
src/api.rs     Axum 路由与处理器（统一 400 错误）
src/main.rs    启动入口
tests/         求解器/规则/随机对照测试与 HTTP 集成测试
examples/      请求样例与 smoke.sh
```

## 非目标与局限

- 不做前端界面；不持久化任何数据（每次请求独立计算）。
- 不实现 SPDX 许可证匹配运算符（`+` 后缀版本范围、LicenseRef、
  `DocumentRef`、注释 `/*..*/` 等）；`+` 仅作为 ID 的一部分接受。
- 不做许可证文本/义务（归属、源码提供等）层面的推理，不输出法律结论。
- 回溯求解器面向中小规模策略图（带 MRV 与传播剪枝）；未针对超大图做
  专门优化，穷举有 `exhaustive_limit` 上限（命中后结果为下界并告警）。
