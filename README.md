# incr-build-planner — 增量构建依赖图规划服务

纯后端的**本地构建规划服务**：输入文件摘要、构建规则与依赖图，输出本次应该执行的
构建动作。用 Rust + Axum 实现，无界面、无状态（不持久化任何东西，上次状态由调用方
随请求回传）。

它**不真正执行命令**，只做规划：哪些构建节点缓存命中、哪些需要重建、按什么顺序执行。

---

## 1. 它解决什么问题

给定一张构建依赖图，在两次构建之间判断：

1. **内容变更**：文件内容哈希变了（含新增、删除），或构建规则变了
   （命令 / 工具版本 / 声明的环境变量）；
2. **仅时间戳变更**：文件内容哈希没变，只是 `mtime` 变了（`touch`、rsync 保内容刷新时间）
   —— 这种情况**不触发任何重建**；
3. 从变更点出发沿依赖边前向传播，求出全部**受影响节点**；
4. 输出受影响构建动作的**拓扑序执行计划**，同一节点无论被多少条路径依赖
   （菱形 DAG 的汇聚点）**只出现一次**；
5. 图中存在环时，**定位环**并给出环上的节点与边。

## 2. 目录结构与依赖

```
Cargo.toml
Cargo.lock                  # 锁定依赖（已提交）
src/
  main.rs                   # 二进制入口（启动 HTTP 服务）
  lib.rs                    # 库入口
  graph.rs                  # 图模型、静态校验、Tarjan 环定位、Kahn 拓扑排序
  engine.rs                 # 缓存键、变更分类、受影响传播、计划生成（纯函数）
  api.rs                    # Axum 路由 / JSON 错误处理
tests/
  planner.rs                # 引擎端到端测试（12 个，含“增量==全量”性质测试）
  http.rs                   # HTTP 层测试（6 个）
examples/
  01_cold_build.json        # 冷构建（全量）
  02_change_leaf.json       # 修改叶输入 -> 只重建一条链
  03_touch_only.json        # 只改 mtime -> 零动作
  04_tool_version_change.json # 工具版本变化 -> 缓存键失效并传播
  05_unrelated_file.json    # 图外无关文件变化 -> 零动作
  06_cycle_validate.json    # 含环图 -> 定位环
  run_demo.sh               # 一键构建 + 启动 + 跑全部场景
```

依赖（均为 Rust 生态常用库，无 C 依赖）：

| crate | 版本 | 用途 |
| --- | --- | --- |
| axum | 0.8 | HTTP 框架 |
| tokio | 1 | 异步运行时（`rt-multi-thread`/`net`） |
| serde / serde_json | 1 | JSON 序列化 |
| sha2 | 0.10 | 构建缓存键的 SHA-256 摘要 |
| tower（dev） | 0.5 | HTTP 测试用 `oneshot` |

## 3. 启动命令与依赖

依赖：Rust 工具链（开发使用 1.98.1，edition 2021；最低不需要特殊特性，stable 即可）。

```bash
# 开发运行
cargo run

# release 运行
cargo run --release
# 或
cargo build --release
./target/release/incr-build-planner
```

默认监听 `http://127.0.0.1:8080`。用环境变量改地址：

```bash
BIND_ADDR=0.0.0.0:9000 ./target/release/incr-build-planner
```

跑测试：

```bash
cargo test
```

Clippy：

```bash
cargo clippy --all-targets
```

## 4. HTTP 接口

### `GET /health`

健康检查。返回 `{"status":"ok","service":"incr-build-planner","version":"0.1.0"}`。

### `POST /plan` — 计算增量构建计划

请求体：

```json
{
  "graph": {
    "nodes": [
      { "id": "left.in",  "kind": "file" },
      {
        "id": "compile_l",
        "kind": "build",
        "rule": {
          "command": "cc -c left.in -o left.o",
          "tool": "cc",
          "tool_version": "13.2.0",
          "env": { "CFLAGS": "-O2" }
        }
      },
      { "id": "app.bin", "kind": "file" }
    ],
    "edges": [
      { "from": "left.in",   "to": "compile_l" },
      { "from": "compile_l", "to": "app.bin" }
    ]
  },
  "previous": {
    "files": {
      "left.in": { "content_hash": "h1", "mtime_ms": 1000 }
    },
    "build_keys": {
      "compile_l": "sha256:..."
    }
  },
  "current": {
    "files": {
      "left.in": { "content_hash": "h2", "mtime_ms": 2000 }
    }
  }
}
```

字段说明：

- `graph.nodes[].kind`：`file`（输入或产物）或 `build`（构建动作）；
  `build` 必须带 `rule`，`file` 不允许带。
- `rule`：`command`（必填）、`tool`、`tool_version`、`env`（键值对）。
  这四项共同决定该构建节点的**缓存键**：
  `sha256(command | tool | tool_version | env 按 key 排序)`。
- `graph.edges[]`：`from -> to` 表示数据/构建顺序依赖，`to` 需要先完成 `from`。
  重复边自动合并。
- `previous`：上一次成功构建时的快照，省略（或为空）即**冷构建**。
  - `previous.files`：上次的文件摘要；
  - `previous.build_keys`：上次每个 build 节点的缓存键（直接取上一次响应里
    `builds[].cache_key` 回传即可），用于识别规则/工具版本/环境变化。
- `current.files`：当前文件摘要。`content_hash` 为空视为“无观测”（用于新增/删除判定）；
  `mtime_ms` 为 Unix 毫秒。

响应（节选）：

```json
{
  "cold_build": false,
  "steps": ["compile_l"],
  "full_order": ["compile_l"],
  "affected_nodes": ["app.bin", "compile_l", "left.in"],
  "content_changed_files": ["left.in"],
  "timestamp_only_files": [],
  "unrelated_files": [],
  "builds": {
    "compile_l": {
      "node_id": "compile_l",
      "cache_key": "sha256:...",
      "reason": { "code": "input_changed", "triggered_by": ["left.in"] }
    }
  },
  "node_states": { "...": "..." },
  "stats": {
    "total_nodes": 3, "total_builds": 1,
    "scheduled_builds": 1, "full_build_count": 1,
    "timestamp_only_files": 0, "changed_inputs": 1, "changed_rules": 0
  }
}
```

- `steps`：本次**按拓扑序**执行的构建动作（已去重，可直接顺序执行）；
- `full_order`：全量构建时的拓扑序，作为对照基准；
- `builds[].reason`：
  - `cache_miss`：冷构建或新增节点；
  - `rule_changed`：规则/工具版本/环境变化（附新旧缓存键）；
  - `input_changed`：上游内容变化传播到它（附直接触发者）；
  - 无 `reason` 字段 = 缓存命中，不执行。

错误：

- `400`：请求体不是合法 JSON；
- `422`：图静态非法（重复 id、悬空边、build 缺 rule 等）或**图中有环**。
  环错误形如：

  ```json
  {
    "error": "dependency graph contains 1 cycle(s)",
    "cycles": [{
      "nodes": ["codegen", "gen", "parse", "opt"],
      "edges": [["codegen","gen"],["gen","parse"],["parse","opt"],["opt","codegen"]]
    }]
  }
  ```

### `POST /validate` — 只校验图 / 定位环

请求体 `{ "graph": { ... } }`。始终返回 200，响应里 `valid` 表示是否合法无环；
`issues` 为静态问题，`cycles` 为定位到的环，`topo_order` 为无环时的拓扑序。

### curl 样例

```bash
# 健康检查
curl -s http://127.0.0.1:8080/health

# 冷构建（全量基准）
curl -s -X POST http://127.0.0.1:8080/plan \
  -H 'content-type: application/json' \
  --data-binary @examples/01_cold_build.json

# 校验含环图
curl -s -X POST http://127.0.0.1:8080/validate \
  -H 'content-type: application/json' \
  --data-binary @examples/06_cycle_validate.json
```

`examples/02`–`05` 的 `previous.build_keys` 是占位符，需要用 01 响应里的真实缓存键
回填。直接跑一键脚本即可自动完成：

```bash
./examples/run_demo.sh
# 输出写到 examples/results/（该目录被 gitignore）
```

## 5. 算法说明

- **变更分类**：逐文件比较 `content_hash` 区分 内容变更 / 新增 / 删除；
  哈希相同但 `mtime_ms` 不同归入 `timestamp_only_files`，不进入种子集。
- **缓存键**：对 build 节点的 `command + tool + tool_version + env` 做带长度前缀的
  SHA-256，env 用有序 map 消除声明顺序影响。与 `previous.build_keys` 比较，
  不同则该节点为规则变更种子。
- **受影响传播**：内容变更文件 + 规则变更 build 作为种子，沿邻接表 BFS 前向标记，
  天然处理菱形（汇聚节点只标记一次）。
- **执行计划**：Kahn 拓扑排序（同层按 id 字典序，输出确定），全量序列过滤出受影响的
  build 节点 —— 因此 `steps` 既是全量顺序的合法子序列，又不含重复节点。
- **环定位**：Tarjan 强连通分量；大小 ≥2 的 SCC 或带自环的单点 SCC 即环，
  再在 SCC 内 DFS 出一条具体环，返回构成它的节点序列和边。

## 6. 验收点对照

| 验收要求 | 对应测试 | 结果 |
| --- | --- | --- |
| 修改叶输入只重建受影响链 | `modifying_a_leaf_rebuilds_only_its_chain_and_link_runs_once` | ✅ |
| 对照全量构建结果 | `incremental_execution_matches_full_execution_after_many_scenarios`（7 个场景下增量执行产物与全量逐节点一致） | ✅ |
| 菱形依赖只执行一次 | `both_leaves_change_but_link_still_runs_once_diamond` | ✅ |
| 环可定位 | `cycle_is_rejected_and_located`、`two_disjoint_cycles_both_reported`、`self_loop_is_located` | ✅ |
| 内容 vs 仅时间戳 | `touch_only_mtime_change_schedules_nothing` | ✅ |
| 缓存键覆盖工具版本与声明环境 | `tool_version_and_declared_env_are_part_of_cache_key`、`tool_version_change_is_reported_as_rule_change_in_plan` | ✅ |
| 无关文件变化 | `unrelated_file_change_is_ignored` | ✅ |
| 修改构建规则 | `changing_rule_command_invalidates_cache_and_propagates` | ✅ |
| 删除输入 | `deleting_an_input_rebuilds_dependents` | ✅ |
| 冷构建=全量、构建后收敛 | `cold_build_schedules_everything_in_topo_order`、`incremental_plan_converges_after_applying_changes` | ✅ |
| HTTP 行为 | `tests/http.rs` 6 个（health/冷构建/touch/环 422/validate/坏 JSON 400） | ✅ |

共 **25 个测试**（图算法单测 7 + 引擎端到端 12 + HTTP 6）。

## 7. 已知边界 / 未完成项

如实记录：

- 服务**不执行命令**，只做规划；调用方按 `steps` 顺序自行执行，并把新的
  `builds[].cache_key` 存入下一次的 `previous.build_keys`。
- 请求只携带**一张当前图**。若图结构本身在两次构建间变化（增删节点/边），当前实现：
  新出现的 build 节点按 `cache_miss` 处理；被删除的节点不再出现；**沿用的边变化**
  （仅改依赖关系、规则不变）不会单独作为变更种子——这是有意简化，若需要“边变化即
  失效”，可在 rule 里编码依赖列表或扩展快照携带图哈希。
- `previous.build_keys` 依赖调用方如实回传；服务端不持久化（保持无状态）。
- 没有鉴权、限流和并发写保护——定位为本地规划服务，默认只监听 `127.0.0.1`。
- 环检测的 Tarjan 是递归实现，图极深（数万节点链）时可能爆栈；常规构建图规模无虞。
- 没有接入真实文件系统扫描（按需求只接收摘要）；`content_hash` 由调用方计算。
