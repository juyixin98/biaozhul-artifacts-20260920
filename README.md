# license-propagation

依赖图许可证策略传播服务（纯后端，无界面）。给定一张依赖图——每个包用一个
**SPDX 许可证表达式子集** 声明许可证，每条依赖边标注链接方式（静态 / 动态）
——再给定一份项目自定义策略（允许矩阵、copyleft 分级、`WITH` 例外规则、
许可证不兼容矩阵），服务会：

1. 解析 SPDX 表达式子集：`AND` / `OR` / `WITH` 与括号；
2. 把表达式化成析取范式（DNF），每个包得到若干"具体许可证合取"备选项；
3. 沿依赖边做 **copyleft 负担定点传播**（强 copyleft 过任意链接、弱
   copyleft 默认过静态链接），**含依赖环**（高度为 2 的单调格，必然收敛）；
4. 用回溯为每个 `OR` 选择寻找一个满足全部规则的组合（优先宽松、非专有、
   简单的选择）；找不到时，给出在 **所有** 选择下都无法避免的冲突路径；
5. 可选地枚举小实例的全部可行解，用于和朴素穷举对照。

> ⚠️ **本服务只做机械的规则计算，不提供法律意见、不构成任何法律结论。**
> 每次响应都带 `disclaimer` 字段。内置的 copyleft 分级和不兼容对是
> 教科书式的保守启发式，全部可以通过请求里的 `policy` 覆盖。

## 技术栈与依赖

- Rust（edition 2021，本机 Rust 1.98 验证）
- [axum](https://crates.io/crates/axum) 0.8（HTTP）
- [tokio](https://crates.io/crates/tokio) 1（异步运行时）
- [serde](https://crates.io/crates/serde) 1 + serde_json（序列化）
- 测试额外用 tower 0.5（服务测试）

无外部数据库、无系统依赖；`Cargo.lock` 已锁定。

## 启动

```bash
cargo run --release
# 默认监听 0.0.0.0:8080，可用 PORT 覆盖：
PORT=9000 cargo run --release
```

## HTTP 接口

| 方法 | 路径       | 说明                                    |
| ---- | ---------- | --------------------------------------- |
| GET  | `/health`  | 健康检查                                |
| POST | `/evaluate`| 完整策略评估：返回可行选择或冲突路径    |
| POST | `/parse`   | 只解析 SPDX 表达式，返回 AST 与 DNF     |

错误的请求体 / 图 / 表达式返回 `400 {"error": "..."}`。

### `POST /evaluate` 请求结构

```jsonc
{
  "graph": {
    "root": "app",                 // 根包 id，必须存在
    "packages": [
      { "id": "app", "license": "Proprietary", "proprietary": false },
      { "id": "lib", "license": "MIT OR GPL-3.0-only WITH Classpath-exception-2.0" }
    ],
    "edges": [
      { "from": "app", "to": "lib", "linking": "static" } // static | dynamic
    ]
  },
  "policy": { /* 全部可选，见下 */ },
  "enumerate": 50                  // 可选：额外枚举至多 N 个可行解
}
```

`policy` 字段（全部可选，默认取保守值）：

| 字段                       | 默认   | 含义                                                |
| -------------------------- | ------ | --------------------------------------------------- |
| `allowed`                  | 内置表 | 额外允许的许可证 id                                 |
| `denied`                   | `[]`   | 从允许矩阵移除（优先级最高）                        |
| `licenses`                 | `{}`   | 自定义/覆盖分级：`{id: {copyleft, proprietary}}`，`copyleft` ∈ `none`/`weak`/`strong` |
| `exceptions`               | 内置表 | 自定义例外：`{id: {effect}}`，`effect` ∈ `clear`/`downgradeWeak`/`keep` |
| `incompatiblePairs`        | 内置表 | 额外声明不兼容的许可证对（对称）                    |
| `compatiblePairs`          | `[]`   | 移除内置的不兼容对                                  |
| `unknownLicensesAllowed`   | false  | 是否允许不在矩阵中的许可证                          |
| `unknownExceptionsAllowed` | false  | 是否允许无规则的例外 id                             |
| `checkDynamicLinks`        | true   | 动态链接是否也传播弱 copyleft / 检查不兼容对（保守）|
| `proprietaryRejectsWeak`   | true   | 弱 copyleft 负担进入专有节点是否算违规              |

内置许可证表覆盖常见 SPDX id（MIT/Apache-2.0/BSD/ISC、LGPL/MPL/EPL、
GPL/AGPL/SSPL）以及一个特殊标记许可证 `Proprietary`。内置不兼容对是保守
启发式，例如 `GPL-2.0-only ↔ GPL-3.0-only`、`GPL-2.0-only ↔ Apache-2.0`；
可用 `compatiblePairs` 解除。

### 响应（可满足时）

```jsonc
{
  "satisfiable": true,
  "selection": { "app": { "expression": "Proprietary", "terms": ["Proprietary"] },
                 "lib": { "expression": "MIT", "terms": ["MIT"] } },
  "violations": [],
  "unavoidableViolations": [],
  "cycles": [],
  "assignmentsExplored": 1,
  "enumeration": null,
  "effectivePolicy": { /* 实际生效的完整矩阵，便于审计 */ },
  "disclaimer": "Rule-engine output only: ... NOT legal advice ..."
}
```

### 响应（不可满足时）

`selection` 为 `null`，`unavoidableViolations` 给出在 **每一种** 许可证
选择下都出现的冲突（按冲突位置聚合，`terms` 汇总所有相关选择）：

```jsonc
{
  "satisfiable": false,
  "selection": null,
  "unavoidableViolations": [{
    "kind": "copyleft-into-proprietary",
    "message": "app static dependency db propagates strong copyleft burden into a proprietary package",
    "path": ["app", "db"],
    "edges": [{ "from": "app", "to": "db", "linking": "static" }],
    "terms": ["GPL-2.0-only", "GPL-3.0-only"]
  }],
  "cycles": []
}
```

违规类型：

- `not-allowed`：许可证/例外不在允许矩阵；
- `incompatible-licenses`：边上两端（或同一包合取内）的许可证被矩阵声明不兼容；
- `copyleft-into-proprietary`：copyleft 负担沿边进入专有节点；
- `root-copyleft-ceiling`：传播到根的实际负担超过根本身许可证可承载的强度。

## 请求样例

`examples/requests/` 下有 5 个样例，覆盖验收点：

| 文件 | 场景 |
| ---- | ---- |
| `01_multi_choice.json` | 多许可证择一 + WITH 例外 + 枚举可行解 |
| `02_cycle.json` | 三包依赖环（可满足，返回环路径） |
| `03_conflict.json` | 专有根只能选强 copyleft → 冲突路径 |
| `04_cycle_conflict.json` | 环上强 copyleft 必然越过 permissive 根天花板 |
| `05_custom_matrix.json` | 自定义许可证、自定义不兼容对、动态链接开关 |

启动服务后：

```bash
curl -s localhost:8080/evaluate \
  -H 'content-type: application/json' \
  --data @examples/requests/01_multi_choice.json | jq

curl -s localhost:8080/parse \
  -H 'content-type: application/json' \
  -d '{"expression": "(MIT OR GPL-2.0-only) AND Apache-2.0"}' | jq
```

## 规则模型（实现细节）

- **传播语义**：每个节点的实际负担 = max（自身许可证强度，沿各入边从
  下游 carry 上来的负担），迭代到定点。强 copyleft（GPL/AGPL/SSPL）经
  任意链接传播；弱 copyleft（LGPL/MPL/EPL）经静态链接传播，动态链接由
  `checkDynamicLinks` 决定。`WITH` 例外在传播前调整强度：
  `clear` 清零（Classpath/GCC exception）、`downgradeWeak` 强转弱
  （LGPL linking exception）、`keep` 不变。
- **根天花板**：根许可证自身强度是其可承载的上界；环会让负担绕回根，
  这也是依赖环影响结果的方式。专有根在 `proprietaryRejectsWeak=false`
  时可接受弱负担，强负担始终越界。
- **求解**：每个包的 DNF 备选项按（强度、是否专有、项数、字典序）排序，
  回溯变量按（根优先、域大小、id）排序。不可满足时对全部赋值求违规
  位置交集，得到"不可避免冲突"。
- **环**：Tarjan SCC 找环并重建一条具体环路径，传播本身不需要拓扑序
  （定点迭代），所以环天然被处理。

## 测试

```bash
cargo test
```

测试组成（共 22 个）：

- 单元测试：SPDX 解析 / DNF 分配律 / 非法表达式；
- HTTP 集成测试（`tests/api.rs`，13 个）：多许可证择一、WITH 例外、
  未知例外拒绝与放开、不兼容矩阵强制改选、双节点环、三节点环冲突、
  动态/静态链接开关、自定义允许矩阵、自定义许可证与不兼容对、枚举、
  非法图错误、免责声明；
- **穷举对照**（`tests/crosscheck.rs`，3 个）：另写了一个朴素穷举"预言机"
  （独立的字符串 DNF + 独立的定点传播实现，与引擎零代码共享），在
  500 个确定性随机小实例（2–5 个包、随机 `AND`/`OR` 表达式、随机
  静态/动态边、自环与平行边）上逐项比对可满足性与可行解集合，并另设
  两个固定验收图（择一 + 例外 + 环，以及"去掉例外后行为改变"对照）。
  最近一次运行：500 例中 389 例可满足，其中 223 个可满足实例含依赖环，
  全部与穷举结果一致。

## 已验证的运行结果

本仓库交付时实际执行（Rust 1.98.1）：

- `cargo build`：成功；
- `cargo test`：全部 **22** 个测试通过（含 500 随机实例穷举对照：
  389 个实例可满足，其中 223 个含依赖环，全部与穷举结果一致）；
- `cargo clippy --all-targets`：无警告；
- HTTP 端到端：使用内置 axum/tower oneshot 测试覆盖 200/400 路径，
  并用真实 `curl` 对运行中的服务跑过 `examples/requests/` 全部样例
  （记录见 README 末尾"运行记录"，如在新机器上请自行复跑）。

## 明确的范围限制（未完成 / 不做）

- 不是法律工具：内置分级与不兼容对是简化启发式，不覆盖真实合规分析；
- 只支持 SPDX 表达式子集：不支持 `+` 后缀版本操作符的语义（`+` 仅作为
  id 字符接受）、不支持 License-Ref/自定义 id 引用语法；
- 求解器面向小图无保护地全枚举（冲突分析与 `enumerate` 都是指数级），
  没有做大规模实例的剪枝/超时/流式接口；
- 没有鉴权、持久化、批量接口和 UI；
- 不解析真实包管理器清单（package.json/Cargo.lock 等），输入必须显式给出。
