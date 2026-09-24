# robot_param_atomic — 机器人参数原子更新（C++17 / rclcpp / SQLite）

纯后端 ROS 2 节点，对三个机器人参数做**原子批量更新**：

| 参数 | 字段 | 约束 |
| --- | --- | --- |
| 采样率 | `sampling_rate_hz` | 有限实数，`> 0` |
| 缓存长度（秒） | `cache_length_s` | 有限实数，`>= 0` |
| 允许延迟（秒） | `allowed_latency_s` | 有限实数，`>= 0` |

**跨字段不变量（对完整快照整体校验，绝不逐字段放行）：**

```
cache_length_s >= 2 * allowed_latency_s
```

满足全部需求：

- **全成或全败**：一次请求携带整组参数，跨字段校验 + SQLite 事务，任何一步失败都整体回滚，内存与磁盘都不留半成品。
- **预期配置版本（乐观锁 / CAS）**：每个请求带 `expected_version`，只有等于当前已提交版本才落盘，否则返回 `REJECT_STALE_VERSION`，防止并发覆盖。
- **回调只读同一完整快照**：提交成功后在互斥内回调，收到的是不可变的旧/新整快照；回调内再次提交会被明确拒绝（`REJECT_UPDATE_IN_CALLBACK`），不会死锁、不会读到撕裂状态。
- **重启加载最近已提交配置**：append-only 历史表，每行带规范 JSON 字节的 **SHA-256（OpenSSL EVP 真实计算）**；启动时最新记录哈希不符或语义非法即拒绝并给出明确状态 `CORRUPT_REJECTED`，回退到最近一条完好记录（全坏则回退默认值）。
- **参数客户端 + 并发测试**：含命令行客户端（`get/set/patch/race`）与 24 个自动化测试，覆盖「单字段合法但组合非法」「回调中更新」「旧版本提交」「持久化失败」。

---

## 1. 环境与依赖

- Ubuntu 24.04 + ROS 2 **Jazzy**
- C++17（GCC 13）、CMake、colcon
- SQLite 3（直接链接 `libsqlite3`，非 mock）
- OpenSSL 3（`libcrypto`，真实 SHA-256）

精确锁定版本见 [`dependencies.lock`](dependencies.lock)。一键安装：

```bash
sudo apt-get update
sudo apt-get install -y \
  ros-jazzy-rclcpp ros-jazzy-std-msgs \
  ros-jazzy-rosidl-default-generators ros-jazzy-rosidl-default-runtime \
  ros-jazzy-ament-cmake ros-jazzy-ament-cmake-gtest \
  ros-jazzy-ros2cli ros-jazzy-ros2run ros-jazzy-ros2param \
  ros-jazzy-ros2service ros-jazzy-ros2launch \
  libsqlite3-dev libssl-dev libgtest-dev \
  cmake g++ python3-colcon-common-extensions
```

---

## 2. 构建

工作区根目录是 `robot_param_ws/`：

```bash
cd robot_param_ws
source /opt/ros/jazzy/setup.bash
colcon build --packages-select robot_param_atomic --cmake-args -DBUILD_TESTING=ON
source install/setup.bash
```

---

## 3. 本地启动

终端 A —— 启动节点（默认数据库文件可用 ROS 参数 `db_path` 指定）：

```bash
source /opt/ros/jazzy/setup.bash
source robot_param_ws/install/setup.bash
ros2 run robot_param_atomic robot_config_node \
  --ros-args -p db_path:=/tmp/robot_config.db
# 或使用 launch 文件：
# ros2 launch robot_param_atomic config_node.launch.py db_path:=/tmp/robot_config.db
```

终端 B —— 客户端操作：

```bash
source /opt/ros/jazzy/setup.bash
source robot_param_ws/install/setup.bash

# 读取当前完整快照
ros2 run robot_param_atomic robot_config_client get

# 整组原子更新（CAS：expected_version 必须是当前版本）
ros2 run robot_param_atomic robot_config_client set \
  --expected 0 --rate 200 --cache 2.0 --latency 0.5

# 部分更新：省略的字段传 -1（服务端仍对合并后的完整快照做不变量校验）
ros2 run robot_param_atomic robot_config_client patch \
  --expected 1 --cache 2.5 --latency 0.9

# 并发压测：8 个独立客户端用同一 expected_version 同时提交，恰好一个成功
ros2 run robot_param_atomic robot_config_client race \
  --expected 2 --threads 8 --rate 500 --cache 3 --latency 1
```

客户端输出单行 JSON；**退出码**：`0` 接受 / `1` 被服务端拒绝 / `2` 传输或用法错误。
节点不在默认命名空间时加 `--ns /你的命名空间`。

ROS 参数是已提交快照的**只读镜像**（便于 `ros2 param get`），直接写会被拒绝：

```bash
ros2 param get /robot_config sampling_rate_hz          # 500.0
ros2 param set /robot_config sampling_rate_hz 1.0      # 拒绝，提示走原子服务
```

---

## 4. 服务协议

自定义接口（`srv/`）：

- **`robot_config/update`** —— `UpdateConfig.srv`
  请求：`expected_version` + 三字段（整组；某字段 `-1` 表示保持不变）。
  响应：`ok / code / message` + 尝试后的版本与完整快照。
- **`robot_config/get`** —— `GetConfig.srv`
  返回完整快照、`config_hash`（SHA-256 hex）、单调 `commit_seq`。

返回码：

| code | 常量 | 含义 |
| --- | --- | --- |
| 0 | `OK` | 已提交 |
| 1 | `REJECT_CONSTRAINT` | 单字段或跨字段校验失败 |
| 2 | `REJECT_STALE_VERSION` | `expected_version` 与当前版本不符 |
| 3 | `REJECT_UPDATE_IN_CALLBACK` | 在变更回调内尝试提交 |
| 4 | `ERR_PERSISTENCE` | SQLite 写入失败，事务已回滚 |
| 5 | `ERR_INTERNAL` | 其它内部错误 |

示例输入见 [`examples/requests.txt`](examples/requests.txt)。

---

## 5. 自动化测试

### 5.1 单元 + 集成（gtest / colcon）

```bash
cd robot_param_ws
source /opt/ros/jazzy/setup.bash
colcon test --packages-select robot_param_atomic
colcon test-result --verbose          # 查看明细
```

共 **24** 个用例，全部通过：

- `test_config_store`（15）：字段/跨字段校验、部分更新、CAS 版本、回调不可变快照、
  回调内提交拒绝、持久化失败回滚、篡改最新行/全坏/语义非法记录恢复、
  8 线程并发 CAS 恰好一胜、SHA-256 与系统 `sha256sum` 已知答案比对。
- `test_config_node_integration`（9）：真实 FastDDS loopback 服务调用，多线程执行器，
  10 个独立客户端并发 CAS、重启加载、直接参数写拒绝、故障注入回滚等。

### 5.2 一键端到端验收脚本

脚本会自行构建（`--build`）、在**隔离的 `ROS_DOMAIN_ID`** 起停真实节点，
断言全部需求场景，并保留临时目录日志/数据库：

```bash
cd robot_param_ws
./src/robot_param_atomic/scripts/acceptance.sh --build
# 已构建并 source 过时可直接：
./src/robot_param_atomic/scripts/acceptance.sh
```

退出码为失败断言数，`0` 表示全部通过。

### 5.3 手动复现关键失败路径

```bash
# 持久化失败（故障注入：强制存储层报错，验证全败回滚）
PARAM_ATOMIC_FAULT=commit_io ros2 run robot_param_atomic robot_config_node \
  --ros-args -p db_path:=/tmp/fault.db
ros2 run robot_param_atomic robot_config_client set --expected 0 --rate 200 --cache 2 --latency 0.5
# -> code 4；get 仍为 v0；数据库中 0 行

# 损坏记录（篡改最新行的载荷但保留旧哈希）
python3 -c "import sqlite3;c=sqlite3.connect('/tmp/robot_config.db');\
c.execute('UPDATE config_commits SET sampling_rate_hz=9999 WHERE id=(SELECT max(id) FROM config_commits)');c.commit()"
ros2 run robot_param_atomic robot_config_node --ros-args -p db_path:=/tmp/robot_config.db
# -> 日志 [ERROR] [load] CORRUPT_REJECTED: ... SHA-256 mismatch ...；get 回退到上一条完好版本
```

---

## 6. 持久化与完整性设计

- 表 `config_commits` 为 **append-only**：`id`（=`commit_seq`）、`version`、三字段、
  `hash`、`created_at`。每次成功提交插入新版本行，旧版本保留可审计。
- `hash = SHA-256(canonical_json)`，规范 JSON 固定键序、double 用 `%.17g`（可精确往返），
  跨进程字节稳定，因此任何字段或版本被篡改都会在启动时被检出。
- 写入使用 `BEGIN IMMEDIATE ... COMMIT`，`synchronous=FULL`、journal 模式 `DELETE`；
  任何失败 `ROLLBACK` 且不更新内存态——真正的全成或全败。
- 启动按 `id DESC` 扫描：哈希不符或语义非法的记录跳过并记录原因，
  取最新一条完好记录；一条都没有则用内置默认值，状态分别为
  `FRESH / LOADED / CORRUPT_REJECTED`，节点日志与服务状态都明确可见。

---

## 7. 目录结构

```
robot_param_ws/
└── src/robot_param_atomic/
    ├── package.xml
    ├── CMakeLists.txt
    ├── dependencies.lock
    ├── srv/
    │   ├── UpdateConfig.srv
    │   └── GetConfig.srv
    ├── include/robot_param_atomic/
    │   ├── config_store.hpp      # 纯 C++17：SQLite + OpenSSL 核心
    │   └── config_node.hpp       # rclcpp 节点封装
    ├── src/
    │   ├── config_store.cpp
    │   ├── config_node.cpp
    │   ├── config_node_main.cpp
    │   └── config_client_main.cpp
    ├── launch/config_node.launch.py
    ├── examples/requests.txt
    ├── scripts/acceptance.sh
    └── test/
        ├── test_config_store.cpp
        └── test_config_node_integration.cpp
```

## 8. 设计说明与边界

- 原子性、版本控制与跨字段约束在服务层统一强制；ROS 参数仅作只读镜像，
  避免单字段直改绕过整体校验。
- 变更回调在 store 互斥内同步执行，保证看到的快照与已提交状态一致；
  回调应快速、非阻塞，且不得向同一 store 提交（会被拒绝）。
- `PARAM_ATOMIC_FAULT` 仅为测试/演练用故障注入开关，生产环境不要设置。
