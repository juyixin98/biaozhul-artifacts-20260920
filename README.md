# depres — 依赖版本约束求解服务（Rust + Axum）

一个**纯后端**的小型包依赖解析服务。给定一个包注册表、根需求、启用的特性、
目标平台和固定选择偏好，它会用**带回溯的 CSP 搜索**找出一个每个包只选一个版本、
且满足全部约束的解；找不到时不会因为一次贪心失败就判无解，而是**穷尽回溯**，
并返回可解释的**冲突链**。

- 语言/框架：Rust 2021 edition + Axum 0.8 + Tokio
- SemVer：自研范围解析器（基于 `semver` crate 的 `Version` 比较），支持
  `^ ~ > >= < <= =`、`* / x` 通配、部分版本（`1`、`1.2`）、空格 AND、`||` OR、
  预发布（npm 风格门控）
- 可选依赖（feature 门控，默认特性名取依赖名，可覆盖）
- 平台条件（`os / arch / family` 上的 `all / any / not` 布尔表达式）
- 固定选择偏好：`highest`（默认）/ `lowest` / 精确 `pin`
- 回溯求解 + MRV 变量选择 + 偏好值排序
- 冲突解释：约束来源链、被拒候选、嵌套子树冲突、循环依赖报告
- 锁文件生成、输入指纹（FNV-1a over canonical JSON）、重放一致性校验
- 内置小规模**穷举 oracle**，用于交叉验证求解器 SAT/UNSAT 结论（`verify:true`）

## 目录结构

```
src/
  semver_range.rs  SemVer 范围语言（解析 + 求值，含预发布门控）
  platform.rs      平台谓词（os/arch/family，all/any/not）
  model.rs         注册表 / 依赖 / 请求 / 响应等线传输类型
  solver.rs        MRV 回溯搜索 + 冲突链构造 + 循环检测
  oracle.rs        小规模暴力穷举 oracle（交叉验证）
  lockfile.rs      锁文件、指纹、重放
  fixtures.rs      内置验收场景
  http.rs          Axum 路由
  main.rs          启动入口
examples/          curl 可直接使用的请求样例
tests/             端到端 HTTP 集成测试（真实启动二进制）
```

## 依赖

见 `Cargo.toml`，锁定版本见 `Cargo.lock`：

- `axum` 0.8（`macros` feature）
- `tokio` 1（`rt-multi-thread`、`macros`、`net`）
- `serde` 1（`derive`）、`serde_json` 1
- `semver` 1（`serde`）

仅需 Rust 工具链（开发使用 1.98.1）。无外部数据库、无系统库依赖。

## 构建与启动

```bash
# 开发构建
cargo build

# 启动（默认监听 0.0.0.0:8080）
cargo run

# 自定义地址
DEPRES_ADDR=127.0.0.1:9000 cargo run --release
```

健康检查：

```bash
curl -s http://127.0.0.1:8080/health
# {"status":"ok","service":"depres"}
```

## 运行测试

```bash
cargo test
```

集成测试会真实编译并启动服务二进制，在临时端口上通过 HTTP 调用。
当前结果：**单元测试 10 个、集成测试 12 个，全部通过。**

## HTTP 接口

### `POST /solve`

请求体（所有字段除 `requirements`/`registry` 外均可省略）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `registry.packages` | `map<name, PackageVersion[]>` | 每个包的已发布版本列表 |
| `requirements` | `[{name, req}]` | 根（应用）需求 |
| `features` | `map<name, string[]>` | 为某包启用的可选特性 |
| `platform` | `{os?, arch?, family?}` | 目标平台，用于条件依赖 |
| `preferences` | `map<name, Preference>` | 每包固定选择偏好 |
| `default_preference` | `"highest"` \| `"lowest"` | 全局默认，缺省 highest |
| `allow_prerelease` | bool | 全局放行预发布版本 |
| `verify` | bool | 是否附穷举 oracle 交叉验证（小实例） |

`PackageVersion`：`{name, version, yanked?, dependencies[]}`
`Dependency`：`{name, req, optional?, feature?, target?}`
`Preference`：`{"strategy":"highest"}` / `{"strategy":"lowest"}`
/ `{"strategy":"pin","version":"1.2.3"}`
`target`：`{"os":"linux"}` / `{"arch":"x86_64"}` / `{"family":"unix"}`
/ `{"all":[...]}` / `{"any":[...]}` / `{"not":{...}}`

响应：
- 有解：`status="satisfiable"`，带 `lockfile`（含版本、依赖范围、来源 `via`、
  输入指纹）、`cycles`（若有环）、`stats`（赋值数/回溯次数/探索节点/耗时/oracle）。
- 无解：`status="unsatisfiable"`，带 `conflict` 冲突树。冲突树每层含
  `package`、施加在该包上的全部 `trace`（来源 `from`/`version`、约束字符串、
  是否可选、平台是否生效）、本帧被拒候选 `rejections`，以及更深层 `nested` 冲突。

### `POST /lock/replay`

请求体：`{request: <SolveRequest>, lockfile: <Lockfile>}`。用相同输入重新求解并与
锁文件逐包比对：
- `status="matches"`：输入指纹一致且解析结果与锁完全相同；
- `status="mismatch"`：给出 `fingerprint_changed`、`missing/extra/changed` 列表，
  或重新求解失败时的 `unsat` 冲突树。

### `GET /fixtures/{name}`

返回内置场景的请求体：`diamond`、`mutex`、`cycle_ok`、`cycle_unsat`、`optional`。

## 请求 / 响应样例

样例文件位于 `examples/`。启动服务后：

### 1. 钻石依赖（贪心失败、回溯成功）

```bash
curl -s -X POST http://127.0.0.1:8080/solve \
  -H 'Content-Type: application/json' \
  -d @examples/diamond.json
```

要点：`a@1.1.0` 要求 `x <1.2`，与 `b` 的 `x >=1.4` 冲突；若先贪心选最高的
`a@1.1.0` 会走入死路。求解器回溯到 `a@1.0.0`（`<1.5`），最终锁定
`a=1.0.0, b=1.0.0, x=1.4.5`。实测 `backtracks=1`，oracle 穷举 7 种组合确认有解。

### 2. 互斥范围（无解 + 冲突链）

```bash
curl -s -X POST http://127.0.0.1:8080/solve \
  -H 'Content-Type: application/json' \
  -d @examples/mutex.json
```

`a` 要求 `x ^1.0`、`b` 要求 `x ^2.0`，无法同时满足。响应冲突树沿决策帧
`a → b → x` 嵌套，叶子 `x` 的 `trace` 同时列出两条互斥约束及其来源。
oracle 穷举 12 种组合确认无解（`satisfiable=false, agrees=true`）。

### 3. 循环依赖（有解，锁中标环）

```bash
curl -s -X POST http://127.0.0.1:8080/solve \
  -H 'Content-Type: application/json' \
  -d @examples/cycle.json
```

`ping ↔ pong` 互相依赖但范围相容。状态为 satisfiable，`cycles=[["ping","pong"]]`，
锁定 `ping=1.0.0, pong=1.1.0`。另有 `cycle_unsat` 场景：环上版本范围互斥时无解。

### 4. 可选依赖 + 平台条件 + 偏好

```bash
curl -s -X POST http://127.0.0.1:8080/solve \
  -H 'Content-Type: application/json' \
  -d @examples/optional_platform.json
```

Linux + 启用 `fast` 特性：选中 `app, core, cache(lowest), io_uring`。
切换到 macOS 且不启用特性：`cache`（未启用）与 `io_uring`（平台不匹配）都被排除，
只剩 `app, core`。

### 5. 锁文件重放

```bash
# 先求解并存下完整响应
curl -s -X POST http://127.0.0.1:8080/solve \
  -H 'Content-Type: application/json' -d @examples/diamond.json > /tmp/sol.json

# 组装 {request, lockfile} 后重放
jq -s '{request:.[0], lockfile:.[1].lockfile}' \
  examples/diamond.json /tmp/sol.json > /tmp/replay.json
curl -s -X POST http://127.0.0.1:8080/lock/replay \
  -H 'Content-Type: application/json' -d @/tmp/replay.json
# {"status":"matches", ...}
```

篡改注册表（删掉 `x` 的 1.4.x）后重放：`status="mismatch"`、
`fingerprint_changed=true`，并携带重新求解的 UNSAT 冲突。

## 求解算法说明

- 把每个受约束的包当作 CSP 变量，域为注册表中满足当前全部约束的版本；
- 变量选择用 **MRV**（候选最少者优先），让冲突尽量靠近根因暴露；
- 值排序遵循固定偏好（highest 降序 / lowest 升序 / pin 精确钉版）；
- 选定一个版本后把它的活跃依赖边作为新约束传播；与已选版本直接冲突则立刻剪枝；
- 子树失败时记录冲突并尝试下一候选，**所有候选耗尽才判无解**，
  返回整棵决策过程的冲突链，而不是“贪心第一选择失败即无解”。

穷举 oracle 对问题可达包宇宙做“每包至多一个版本（可缺席）”的混合进制枚举，
逐组合检查可行性，规模超过 20 万组合会标记 `truncated` 并放弃交叉验证
（此时 `agrees` 不代表结论，仅在未截断时有意义）。

## 已验证的验收点

- 钻石依赖：实测回溯 1 次得到唯一可行解，oracle 7 组合确认 SAT。
- 互斥范围：冲突链叶子列出 `^1.0` 与 `^2.0` 及各自来源，oracle 12 组合确认 UNSAT。
- 循环依赖：相容环 SAT 且锁中标环；环上范围互斥（cycle_unsat）UNSAT。
- 小规模穷举：求解器与 oracle 在全部内置 fixture 上 SAT/UNSAT 结论一致
  （`tests/api.rs::solver_and_oracle_agree_on_every_fixture`）。
- 锁文件重放：相同输入连续重放结果一致；输入漂移时正确报 mismatch 与指纹变化。

## 已知限制 / 未完成项（如实记录）

- 同一包在一次求解中只允许存在一个版本（不支持 npm/pnpm 式多版本共存与
  peer-dedupe）。这是有意的简化，符合“每包单版本”的经典包管理模型。
- 穷举 oracle 仅用于小规模交叉验证，超过 `ORACLE_NODE_LIMIT`（20 万组合）
  会截断并置 `truncated=true`，不参与生产规模求解。
- 没有持久化：注册表与请求每次随 HTTP 提交；服务本身不存数据。
- 锁文件指纹是非加密哈希（FNV-1a），仅用于检测输入漂移，不做防篡改安全保证。
- 仅有自动化测试与 curl 级样例，未提供鉴权、限流、请求体大小限制等运营能力。
- SemVer 解析器未实现连字符区间（`1.2.3 - 2.0.0`）和构建元加权（构建元
  在比较中按规范忽略）；常见 `^ ~ >= <= > < = * x`、AND/OR、预发布均已覆盖并有单测。
