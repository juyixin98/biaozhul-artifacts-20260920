# depsolver — 依赖版本约束求解服务

一个小型纯后端包依赖解析服务：Rust + Axum，支持 SemVer 范围、可选依赖（extras）、
平台条件依赖，采用**带回溯的搜索**（非贪心），无解时输出**冲突链解释**，并生成可
重放校验的锁文件。无界面，仅 HTTP 接口。

## 构建与启动

依赖：Rust 工具链（开发使用 rustc 1.98.1 / cargo 1.98.1），其余依赖见 `Cargo.toml`，
版本由 `Cargo.lock` 锁定（axum 0.7、tokio 1、serde 1、semver 1）。

```bash
cargo build --release
./target/release/depsolver          # 默认监听 127.0.0.1:3000
BIND_ADDR=0.0.0.0:8080 ./target/release/depsolver
```

## 运行测试

```bash
cargo test
```

实测结果（2026-09-24，本机）：**14 个测试全部通过** ——

- `tests/solver_tests.rs`（9 个）：钻石依赖、互斥范围冲突链、回溯（贪心最高版本失败后
  回退）、兼容/不兼容循环依赖、可选依赖 extras、平台条件、锁文件重放一致性、未知包。
- `tests/exhaustive.rs`（1 个）：小规模穷举对照。固定种子随机生成 400 个小型注册表
  （2–3 个包、每包 1–3 个版本、随机依赖/可选/平台条件），将求解器结论与暴力枚举全部
  版本组合的结果逐一比对：**400 例全部一致（188 有解 / 212 无解）**；且有解时求解器
  产出的锁文件均通过 `verify_lock` 重放校验。
- `tests/http_tests.rs`（4 个）：进程内驱动 Axum 路由，覆盖 /health、/resolve
  （有解、无解冲突链、非法范围 400）、/replay 往返与篡改拒绝。

## HTTP 接口

### `GET /health`

```json
{"service":"depsolver","status":"ok","uptime_secs":78}
```

### `POST /resolve`

请求体（完整样例见 `examples/`）：

```json
{
  "registry": { "packages": [
    { "name": "a", "versions": [
      { "version": "1.0.0", "dependencies": [
        { "name": "c", "range": "^1.0.0",
          "optional": "secure",        // 可选：仅当 extras 含 "a/secure" 时生效
          "platform": "linux" } ] } ] }   // 可选：仅当请求 platform 匹配时生效
  ]},
  "requirements": [ { "name": "a", "range": "^1.0.0" } ],
  "platform": "linux",                 // 可选
  "extras": [ "a/secure" ]             // 可选，格式 "<包名>/<extra 名>"
}
```

- 版本号与范围遵循 SemVer（`semver` crate 的 `VersionReq` 语法：`^1.2`、`~1.2`、
  `>=1.0.0, <2.0.0`、`=1.0.0`、`*` 等）。非法范围返回 400。
- 选择偏好固定：候选版本**从高到低**尝试，同版本按注册表顺序，结果确定可复现。

有解时（`status: "solved"`）返回 `locked`（包→版本）、`decisions`（决策/回溯轨迹）、
`stats` 以及可直接喂给 `/replay` 的 `lockfile`。无解时（`status: "unsolvable"`）
返回冲突链：

```json
{
  "status": "unsolvable",
  "conflict": {
    "package": "c",
    "chains": [
      { "range": ">=2.0.0", "required_by": ["<root>", "a@1.0.0"] },
      { "range": "<2.0.0",  "required_by": ["<root>", "b@1.0.0"] }
    ],
    "explanation": "no version of `c` satisfies all constraints: `>=2.0.0` (from <root> → a@1.0.0), `<2.0.0` (from <root> → b@1.0.0)"
  }
}
```

### `POST /replay`

请求体 `{ "registry": ..., "lockfile": </resolve 返回的 lockfile> }`。校验：锁内版本
在注册表中存在、根需求满足、每个锁定包的所有生效依赖（按 lockfile 中的
platform/extras 过滤）均被锁内版本满足。全部通过返回 `{"valid": true, "locked": ...}`；
否则 422 并列出每条违例。

## 求解算法

`src/solver.rs`：递归回溯搜索。

1. 状态 = 已选版本 + 每个包上累积的约束（每条约束带来源链 `<root> → a@1.0.0 → …`）。
2. 每次选候选最少的未决包（确定性平局按包名），按固定偏好（版本降序）逐个尝试。
3. 选定后把其生效依赖（平台/extras 过滤后）加入约束；若与已选版本冲突，记录冲突链
   并回溯尝试下一候选 —— **不会因贪心首选失败直接判无解**。
4. 全部候选耗尽则向上回溯；搜索结束时返回最深的冲突链作为解释。
5. 循环依赖天然终止：每包只选一次，循环表现为对己选包的新约束校验（兼容则通过，
   不兼容则冲突回溯）。

## 请求样例（`examples/`，均已实际运行验证）

```bash
curl -s -X POST localhost:3000/resolve -H 'content-type: application/json' \
     -d @examples/resolve_diamond.json            # 钻石依赖 → c 选 1.5.0
curl -s -X POST localhost:3000/resolve -H 'content-type: application/json' \
     -d @examples/resolve_conflict.json           # 互斥范围 → unsolvable + 冲突链
curl -s -X POST localhost:3000/resolve -H 'content-type: application/json' \
     -d @examples/resolve_backtrack.json          # a@2.0.0 失败 → 回溯到 a@1.0.0
curl -s -X POST localhost:3000/resolve -H 'content-type: application/json' \
     -d @examples/resolve_optional_platform.json  # extras + 平台条件
```

`resolve_backtrack.json` 实测输出（节选），可见回溯轨迹：

```json
{ "status": "solved",
  "decisions": ["try a@2.0.0", "conflict: no candidate for `c`, backtracking",
                "try a@1.0.0", "try c@1.0.0"],
  "locked": {"a": "1.0.0", "c": "1.0.0"},
  "stats": {"backtracks": 1, "decisions": 3} }
```

锁文件重放：对 `resolve_diamond.json` 的 lockfile 调 `/replay` 返回 `valid: true`；
把 `c` 篡改为 `2.0.0` 后返回 422 及两条违例（`a` 的 `^1.0.0` 与 `b` 的 `<2.0.0`）。

## 已知限制 / 未完成项

- 注册表随每次请求提交，无持久化与增量索引；大规模注册表未做性能优化（指数级
  回溯无剪枝缓存，仅按“候选最少”排序）。
- 平台条件是精确字符串匹配（大小写不敏感），不支持 `cfg` 表达式类的复合条件。
- extras 只能全局预先启用（`["包/extra"]`），不支持依赖链中途按版本条件启用。
- 冲突解释返回“最深的一条”冲突链，不枚举全部冲突；无 SAT 式的不可满足核最小化。
- 每个包在解中只有一个版本（不支持同一包多版本共存）。
- `/replay` 只校验锁文件一致性，不检查锁中是否有多余（不可达）包。
