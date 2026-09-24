# robot_param_atomic — 机器人参数原子更新（C++17 / rclcpp / SQLite）

纯后端 ROS 2（Jazzy）服务，对机器人的三个运行参数提供**原子、带版本、可持久化**的批量配置：

| 参数 | 类型 | 含义 | 单字段约束 |
|---|---|---|---|
| `sample_rate_hz` | float64 | 采样率 [Hz] | `0 < x ≤ 1e6`，有限数 |
| `buffer_length` | uint32 | 缓存长度 [样本数] | `0 < x ≤ 1e8` |
| `allowed_latency_ms` | uint64 | 允许延迟预算 [ms] | `0 < x ≤ 86400000` |

**跨字段不变量（真实计算，非字符串规则）**：缓存必须能覆盖至少两倍允许延迟：

```
buffer_length / sample_rate_hz * 1000.0  ≥  2.0 * allowed_latency_ms
```

默认配置 `100 Hz / 1000 samples / 50 ms`（覆盖 10000 ms，要求 100 ms）。

---

## 1. 架构与正确性设计

```
                 ┌──────────────────────────────────────────────┐
 ROS 2 客户端 ──▶ │ RobotConfigNode (MultiThreadedExecutor)      │
 (CLI / 其他节点) │  ├─ ~/update  (srv/AtomicUpdate, Reentrant)  │
                 │  ├─ ~/get     (srv/GetConfig)                 │
                 │  └─ ~/snapshots (msg/ConfigSnapshot, latched) │
                 │         │                                    │
                 │         ▼ 短临界区（仅版本检查+事务）          │
                 │  ┌────────────────────┐  FIFO 单线程分发器    │
                 │  │ ConfigStore        │ ───────────────────▶ │
                 │  │  mutex + 单写者    │  话题发布 + 进程内回调 │
                 │  └─────────┬──────────┘                      │
                 └────────────┼─────────────────────────────────┘
                              ▼
                    SQLite (BEGIN IMMEDIATE 事务)
                    每行: 值 + SHA-256 + HMAC-SHA-256
```

保证的语义：

1. **批量全成或全败**：合并补丁 → 校验（含跨字段约束）→ 在单个 `BEGIN IMMEDIATE … COMMIT` 事务里 INSERT 新版本行。任一步失败立即 `ROLLBACK`，内存快照与磁盘都不变；版本号不被消耗。
2. **乐观并发控制**：请求必须带 `expected_version`。与当前版本不一致返回 `VERSION_CONFLICT`（status=2），并回传当前快照供调用方刷新重试。8 客户端同时竞争时恰好 1 个成功。
3. **回调只读同一完整快照**：提交成功后，快照进入**单线程 FIFO 分发队列**，按版本顺序 (a) 发布 latched 话题、(b) 调用进程内监听器。回调拿到的 `const ConfigSnapshot&` 永远是一次提交的完整不可变快照，看不到"半个批次"，也不会乱序。
4. **回调内更新被拒绝而非死锁**：服务处理线程**从不执行用户回调**（回调在独立分发线程），因此不存在"回调线程等待自己"的死锁。同一进程的监听器用 `origin: "inproc:…"` 标识；分发进行中收到的这种请求立即返回 `REENTRANT_UPDATE`（status=4），不持有任何锁。外部客户端（`origin: ""`）永不受此限制。
5. **重启加载最近已提交配置**：启动时扫描全部版本行（新→旧），每行做**类型/范围检查 + SHA-256 完整性 + HMAC 真实性 + 业务约束**四重验证。最新行坏了就回退到最近的好行（`RECOVERED`），全坏则用内置默认（`NO_VALID_CONFIG`），状态与逐行拒绝原因通过 `~/get` 和启动日志明确给出。数据库文件本身结构性损坏（`PRAGMA quick_check != ok`）直接抛异常、拒绝启动。
6. **真实密码学**：OpenSSL EVP（`EVP_sha256` / EVP_MAC HMAC / `RAND_bytes` / `CRYPTO_memcmp` 常量时间比较）。密钥 32 字节随机，来自 `$ROBOT_PARAM_KEY`（原始字节或 64 位 hex）或自动生成的 `<db>.key`（权限 0600，发现权限过宽会收紧）。测试用 NIST/RFC 4231 官方已知答案向量验证。
7. **持久化失败如实上报**：INSERT/COMMIT 失败返回 `PERSISTENCE_FAILED`（status=3），回滚后服务继续可用；测试用 SQLite 触发器真实注入磁盘故障。

## 2. 协议

### `~/update` — `robot_param_atomic/srv/AtomicUpdate`

请求：`expected_version` + 每个字段的 `set_*` 存在标记（只改出现的字段）+ `origin`。
响应状态码：

| code | 名称 | 含义 | 服务端状态变化 |
|---|---|---|---|
| 0 | OK | 已提交新版本 | 版本 +1 |
| 1 | REJECTED_VALIDATION | 单字段或跨字段约束不满足 | 无 |
| 2 | VERSION_CONFLICT | expected_version 过期 | 无 |
| 3 | PERSISTENCE_FAILED | SQLite 事务失败（已回滚） | 无 |
| 4 | REENTRANT_UPDATE | 回调内发起的同进程更新 | 无 |

### `~/get` — `robot_param_atomic/srv/GetConfig`
返回当前完整快照 + `load_state`（0 FRESH / 1 LOADED / 2 RECOVERED / 3 NO_VALID_CONFIG）与 `load_detail` 文本。

### `~/snapshots` — `robot_param_atomic/msg/ConfigSnapshot`（transient_local, KEEP_LAST 4, reliable）
每次提交一条；节点启动时先 latch 当前快照（v0 或磁盘加载版本）。

## 3. 目录结构

```
src/robot_param_atomic/
├─ CMakeLists.txt  package.xml  dependencies.lock
├─ msg/ConfigSnapshot.msg
├─ srv/AtomicUpdate.srv  srv/GetConfig.srv
├─ include/robot_param/{core,node}/...
├─ src/core/    validation.cpp crypto.cpp store.cpp   # 不依赖 rclcpp
├─ src/node/    robot_config_node.cpp + node_main.cpp
├─ src/client/  robot_config_client.cpp               # 参数客户端 CLI
├─ test/        test_validation / test_crypto / test_store / test_config_node_integration
├─ examples/    *.yaml（4 类示例请求）
└─ scripts/     install_deps.sh smoke_e2e.sh restart_e2e.sh
```

## 4. 本地启动

前置：Ubuntu 24.04 + ROS 2 Jazzy（依赖见 `dependencies.lock`，一键安装见 `scripts/install_deps.sh`）。

```bash
cd robot_param_ws
source /opt/ros/jazzy/setup.bash

# 构建
colcon build --packages-select robot_param_atomic --cmake-args -DCMAKE_BUILD_TYPE=Release
source install/setup.bash

# 启动节点（固定密钥便于重启复用；不传则自动生成 db.key）
export ROBOT_PARAM_KEY=00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff
ros2 run robot_param_atomic robot_config_node \
  --ros-args -r __node:=robot_config -p db_path:=/tmp/rp/robot_param.sqlite3
```

另开终端：

```bash
source install/setup.bash
ros2 run robot_param_atomic robot_config_client get

# 合法提交 @v0
ros2 run robot_param_atomic robot_config_client update --expected-version 0 --buffer 5000

# 单字段都合法但组合非法（5000 样本@100Hz 覆盖 50000ms，要求 120000ms）→ exit 1
ros2 run robot_param_atomic robot_config_client update --expected-version 1 --latency 60000

# 旧版本提交 → exit 2
ros2 run robot_param_atomic robot_config_client update --expected-version 0 --rate 200

# 原始服务调用（示例输入文件）
ros2 service call /robot_config/update robot_param_atomic/srv/AtomicUpdate \
  "$(cat src/robot_param_atomic/examples/update_illegal_combination.yaml)"
```

CLI 退出码即服务状态码（0–4），10 表示传输/参数错误，便于脚本编排。

## 5. 自动化测试与验收命令

```bash
source /opt/ros/jazzy/setup.bash
source install/setup.bash

# ① 全部单元/集成测试（gtest + ROS linters，共 295 项）
colcon test --packages-select robot_param_atomic
colcon test-result --all          # 期望: 0 failures

# 只跑某个套件
./build/robot_param_atomic/test_validation            # 跨字段约束
./build/robot_param_atomic/test_crypto                # NIST/RFC 已知答案
./build/robot_param_atomic/test_store                 # SQLite/损坏/注入故障
./build/robot_param_atomic/test_config_node_integration

# ② 跨进程端到端（真实节点 + 真实 CLI，两个独立进程）
src/robot_param_atomic/scripts/smoke_e2e.sh           # 合法/非法/旧版本/话题
src/robot_param_atomic/scripts/restart_e2e.sh         # 重启加载/篡改回退/异密钥
```

测试矩阵（任务要求的 4 类场景全部覆盖）：

| 场景 | 单元测试 | 集成/端到端 |
|---|---|---|
| 单字段合法、组合非法 | `Validation.SingleFieldsLegalButCombinationIllegal` | `…ViaService`，smoke 第 3 步 |
| 回调中更新 | — | `CallbackSeesCompleteSnapshotAndReentrantUpdateRejected`（断言返回 4、不死锁、快照完整、外部更新事后成功） |
| 旧版本提交 | `StoreTest.StaleVersionIsRejected…` | `StaleVersionCommitIsRejected`、`EightConcurrentClientsExactlyOneWinsRestRetry`（8 并发 1 胜 7 冲突，刷新后全部追平到 v8） |
| 持久化失败 | `StoreTest.PersistenceFailureRollsBack…`（ABORT 触发器）、`UnwritableDatabase…`、`StructuralCorruption…` | `PersistenceFailureReturnsErrorAndServiceSurvives` |
| 重启/损坏 | 篡改行、伪造 HMAC、异密钥、坏 hex、结构性损坏共 6 例 | `RestartLoadsLastCommittedAndReportsCorruption`，restart_e2e 四阶段 |
| 密码学真实性 | SHA-256 ×3（含 100 万 'a' NIST 向量）、HMAC RFC 4231 ×2、篡改/异密钥检测 | 伪造 HMAC 行被拒绝 |

最近一次实测结果：**295 tests, 0 errors, 0 failures**（75 skipped 为 rosidl 生成代码的无关 linter）；8/8 ROS 集成测试、两个 E2E 脚本全部通过。

## 6. 设计说明 / 权衡

- **为何不用 ROS 原生参数机制**：`set_parameters` 没有"批量 + 跨字段 + expected_version + 行级完整性"原语；本设计以服务承载事务语义，SQLite 承载持久化与完整性。
- **HMAC 与 SHA-256 并存**：SHA-256 检测意外位翻转/磁盘损坏；HMAC 防止攻击者插入一条"自洽但非本节点写入"的伪造行。换密钥后旧行全部不可信（`NO_VALID_CONFIG`），这是有意的安全语义。
- **版本号单调不复用**：即使最新行损坏回退到 v1，下一次提交仍是 `max(磁盘版本)+1`（v3），避免版本号二义性。
- **分发异步化**：服务响应在事务落盘后立即返回；快照通知经 FIFO 队列异步串行送达，提交延迟不被慢回调拖垮，且天然消除回调内重入死锁。

## 7. 许可

Apache-2.0。
