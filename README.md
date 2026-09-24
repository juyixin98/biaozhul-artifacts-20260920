# 增量构建依赖图规划服务（Rust + Axum）

纯后端 HTTP 服务：输入**文件摘要 + 构建规则 + 依赖图 + 上次构建快照**，
计算本次需要重建的节点（增量构建方案），并给出全量构建方案做对照。

- **内容变更 vs 仅时间戳变更**：缓存键只由内容决定；`mtime` 变化但 `content_hash`
  不变时不触发任何重建（在响应中单独列出）。
- **缓存键覆盖工具版本与声明环境**：目标缓存键 = SHA-256(节点 id + 规则指纹 + 上游键)，
  规则指纹包含命令、工具名、工具版本、声明环境变量。
- **菱形依赖只执行一次**：沿依赖边单趟拓扑传播，集合去重，汇合点恰好一个构建步骤。
- **环可定位**：迭代式三色 DFS 返回环上的节点路径与边，HTTP 422。
- **增量与全量产物对照**：响应中模拟两套产物并逐节点比对（`simulation.equivalent`），
  同时验证所有跳过节点的缓存命中是否有效（`cache_hits_valid`）。

## 目录

```
src/
  main.rs       启动入口（绑定 127.0.0.1:8080，可用 PORT 覆盖）
  api.rs        Axum 路由：GET /healthz、POST /plan
  model.rs      请求/响应数据模型（serde）
  graph.rs      图校验、环定位、Kahn 拓扑排序
  planner.rs    缓存键、变更识别、影响传播、全量对照模拟
tests/
  api_tests.rs  16 个 HTTP 级集成测试
  common/       测试用菱形图构造与调用辅助
examples/
  01_cold_build.json              冷启动（无 baseline）
  02_leaf_content_changed.json    叶输入内容变更（含真实 baseline 缓存键）
  03_timestamp_only.json          仅 mtime 变更
  04_unrelated_file.json          无关文件
  05_rule_changed.json            构建规则（命令）变更
  06_tool_and_env_changed.json    工具版本 + 声明环境变更
  07_cycle.json                   依赖环（期望 422）
  run_demo.sh                     端到端验收脚本（自动起服务 + curl + 断言）
```

## 依赖

- Rust（开发与实测版本 **1.98.1**，edition 2021；无特殊 nightly 特性）
- 仅 4 个直接运行时依赖：`axum 0.8`、`tokio 1`（rt/net/macros）、`serde 1`、`serde_json 1`、`sha2 0.10`
- 构建期会拉取间接依赖，版本锁定在 `Cargo.lock`
- 运行演示脚本需要 `bash`、`curl`、`jq`

## 启动命令

```bash
cargo run                      # 默认 http://127.0.0.1:8080
PORT=9000 cargo run            # 自定义端口
cargo build --release          # 发布构建
```

健康检查：

```bash
curl http://127.0.0.1:8080/healthz
# {"status":"ok","service":"incremental-build-planner"}
```

## HTTP 接口

### `POST /plan`

请求体：

| 字段 | 说明 |
|---|---|
| `graph.nodes[]` | 节点：`id`、`type`（`input`/`target`）、`depends_on[]`、`rule`（仅 target） |
| `rule.command` | 构建命令 |
| `rule.tool` | `{ "name", "version" }` —— 参与缓存键 |
| `rule.environment` | 声明环境 map（如 `CC`、`CFLAGS`）—— 参与缓存键 |
| `files` | 当前文件摘要 map：`{ content_hash, mtime? }`；**key 必须与 input 节点 id 对应** |
| `baseline` | 上次响应里的 `next_snapshot` 原样带回；首次构建传 `null` |

成功响应（200）核心字段：

| 字段 | 说明 |
|---|---|
| `status` | `full_build`（冷启动）/ `incremental` |
| `topological_order` | 全图拓扑顺序 |
| `changed_inputs[].kind` | `content_changed` / `timestamp_only` / `new_file` |
| `timestamp_only_inputs` | 仅时间戳变化、不触发重建的输入 |
| `rule_changes[].details` | `command_changed` / `tool_name_changed` / `tool_version_changed` / `environment_changed` / `new_rule` |
| `affected_targets` | 受影响（需重建）的目标节点 |
| `incremental.steps[]` | 增量步骤：`{node, reason, cache_key, rule_fingerprint}`；`reason` 为 `cold_build`/`rule_changed`/`dependency_changed`/`new_target` |
| `incremental.skipped[]` | 缓存命中跳过的目标 |
| `full_build.steps[]` | 全量构建步骤（对照基线） |
| `simulation.equivalent` | 增量产物与全量产物是否逐节点一致 |
| `simulation.cache_hits_valid` | 所有跳过节点的基线键与当前键是否一致 |
| `next_snapshot` | **持久化后下次请求原样带回** |
| `ignored_files` | files 中不属于任何图输入的条目（无关文件） |
| `warnings` | 非致命提示 |

错误：

- `400` — `{"error":"invalid_request","errors":[...]}`：未知依赖、目标缺规则、输入节点带规则/依赖、缺文件摘要、JSON 非法（`{"error":"invalid_json",...}`）
- `422` — `{"error":"cycle_detected","cycle":{"nodes":[...],"edges":[...]}}`：`nodes` 首尾重复，可直接定位环

### 请求样例

```bash
# 1) 冷启动：全量构建
curl -s -X POST http://127.0.0.1:8080/plan \
  -H 'content-type: application/json' \
  -d @examples/01_cold_build.json | jq '.incremental.steps'

# 2) 叶输入内容变更（b.o、c.o、app 重建；app 作为菱形汇合点只执行一次）
curl -s -X POST http://127.0.0.1:8080/plan \
  -H 'content-type: application/json' \
  -d @examples/02_leaf_content_changed.json | jq '.affected_targets'

# 3) 仅时间戳：零重建
curl -s -X POST http://127.0.0.1:8080/plan -H 'content-type: application/json' \
  -d @examples/03_timestamp_only.json | jq '.incremental.step_count'   # 0

# 5) 规则变化：只有 app 重建
curl -s -X POST http://127.0.0.1:8080/plan -H 'content-type: application/json' \
  -d @examples/05_rule_changed.json | jq '[.incremental.steps[].node]' # ["app"]

# 7) 环：422 + 环路径
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8080/plan \
  -H 'content-type: application/json' -d @examples/07_cycle.json        # 422
```

`02`–`06` 号样例已内嵌 `01` 冷启动真实响应中的 baseline（真实 SHA-256 缓存键），
可直接按顺序之外的任意顺序单独发送。

## 缓存键设计

```
leaf_key   = sha256("leaf-v1|"   + content_hash)
rule_fp    = sha256("rule-v1|"   + canonical_json({command, tool{name,version}, environment}))
target_key = sha256("target-v1|" + node_id + "|" + rule_fp + "|" + dep_key_1 + ";" + dep_key_2 ...)
```

- 输入键与 `mtime` 无关 → 仅时间戳变化不影响任何下游键；
- 规则使用确定性 JSON 序列化（`environment` 为 `BTreeMap`），字段顺序不同不会产生不同键；
- 带类型前缀（`leaf`/`rule`/`target`）做域分离；
- 目标键嵌入全部上游当前键（按依赖列表顺序），上游任一内容/规则变化都会级联。

> 说明：本服务是**规划器**，不执行真实构建命令。为支持“对照全量构建结果”验收，
> 服务把内容寻址的 `target_key` 同时作为“产物摘要”做模拟：全量 = 当前键重算全部目标，
> 增量 = 受影响目标用当前键、跳过目标沿用基线键。真实构建系统接入时，可将
> `cache_key` 用作远程/本地缓存查找键，`simulation` 字段的对照思想不变（比较产物哈希）。

## 自动化测试与实际运行结果

```bash
cargo test                 # 全部测试
cargo clippy               # lint（无警告）
bash examples/run_demo.sh  # 起真实服务，7 组场景、29 条断言
```

实测记录（Rust 1.98.1，Linux x86_64，2026-09-24）：

- `cargo test`：**19 passed, 0 failed**（3 个缓存键单元测试 + 16 个 HTTP 集成测试）
- `cargo clippy`：无警告
- `bash examples/run_demo.sh`：**PASS=29 FAIL=0**，覆盖：
  冷启动全量（3 步）、叶内容变更（`b.o,c.o,app`，app 一次）、仅时间戳（0 步）、
  无关文件（0 步，列入 `ignored_files`）、规则命令变更（仅 `app`）、
  工具版本+环境变更（`b.o,c.o,app`）、环（422，路径 `app -> b.o -> app`）；
  所有可构建场景 `simulation.equivalent=true`、`cache_hits_valid=true`。

### 验收点对照

| 验收要求 | 对应测试/场景 |
|---|---|
| 修改叶输入 | 集成测试 `leaf_content_change_propagates_and_matches_full_build`；样例 02 |
| 修改构建规则 | `rule_command_change_rebuilds_only_that_target` / `tool_version_change_rebuilds` / `declared_environment_change_rebuilds`；样例 05、06 |
| 修改无关文件 | `unrelated_file_is_ignored_and_builds_nothing`；样例 04 |
| 对照全量构建结果 | 每个场景断言 `simulation.equivalent` 且两套 artifacts 完全相等；`full_build` 视图并列返回 |
| 菱形依赖只执行一次 | `cold_build...merge_once` 与叶变更测试中统计 app 出现次数恰好 1 |
| 环可定位 | `cycle_is_detected_and_located`（节点路径+边）、`self_loop_is_reported_as_cycle`；样例 07 |
| 区分内容/时间戳 | `timestamp_only_change_builds_nothing`；样例 03 |
| 缓存键覆盖工具版本/环境 | `key_tests` 单元测试 + 样例 06 |

## 未完成项 / 边界说明

- **不执行真实命令**：只做规划与基于缓存键的产物模拟（设计如此，纯后端规划服务）。
- 快照（baseline）由调用方持久化并原样带回，服务本身无状态、不落盘。
- 文件摘要（`content_hash`）由调用方计算后传入；服务不读文件系统。
- `mtime` 仅用于区分时间戳变更，不参与任何缓存键；缺失时按未提供处理。
- 未做鉴权、限流与并发持久缓存（当前为单进程内存计算，计算本身是确定性纯函数）。
