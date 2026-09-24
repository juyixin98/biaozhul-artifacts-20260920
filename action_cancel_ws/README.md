# 动作取消一致性 (Action Cancellation Consistency)

纯后端的 ROS 2 action server：执行**合成分段任务**（不控制真实机器人），
每段执行**真实的密码学计算**（PBKDF2-HMAC-SHA256 + SHA-256 链式摘要），
进度在发布反馈**之前**持久化到 SQLite。它保证：

- **目标 ID 去重**：同 ID 不同参数一律拒绝；同 ID 相同参数不会二次执行。
- **取消/完成竞争只有一个终态**：SQLite 写事务是唯一仲裁者，取消与末段完成
  串行化，结果要么 `SUCCEEDED` 要么 `CANCELED`，不可能两者兼有。
- **已取消目标不能再提交段结果**：取消提交后到达的段结果会被拒绝且不入库，
  终态段数与实际持久化的段记录严格相等。
- **进程中断恢复**：重启时把崩溃前处于 PENDING/RUNNING 的目标显式标记为
  `RECOVERABLE`，再按目标携带的**显式策略**续跑（`RESUME`）或中止（`ABORT`），
  续跑前会重算全部已持久化段做完整性校验。
- **历史查询**：`get_goal` / `list_goals` 服务随时（含重启后）返回目标终态、
  每段记录、链式哈希、拒绝原因等。

技术栈：Python 3.12 · rclpy (ROS 2 Jazzy) · SQLite 3 (WAL) · 无前端。

---

## 1. 目录结构

```
action_cancel_ws/
├── build.sh / test.sh / accept.sh      # 构建 / colcon 测试 / 端到端验收
├── requirements.txt / requirements.lock
├── examples/                            # 示例输入（JSON）
└── src/
    ├── action_cancel_interfaces/        # ament_cmake；.action/.srv/.msg
    │   ├── action/SegmentedTask.action
    │   ├── srv/GetGoal.srv
    │   ├── srv/ListGoals.srv
    │   └── msg/{SegmentRecord,GoalSummary}.msg
    └── action_cancel_server/            # 服务端实现（ament_cmake + python install）
        ├── action_cancel_server/
        │   ├── states.py                # 状态/策略/错误码常量
        │   ├── crypto.py                # 真实 PBKDF2 / SHA-256 计算
        │   ├── storage.py               # SQLite 状态机（竞争仲裁核心）
        │   └── server.py                # rclpy action server + 恢复 + 历史服务
        ├── scripts/segment_server.py    # 服务端入口
        ├── scripts/segment_client.py    # CLI 客户端（send/get/list）
        └── test/                        # 24 个自动化测试
```

---

## 2. 依赖（已锁定）

ROS 侧通过 **apt** 安装，**绝不用 pip 安装 rclpy**：

```bash
sudo apt update
sudo apt install -y \
  ros-jazzy-rclpy ros-jazzy-action-msgs ros-jazzy-builtin-interfaces \
  ros-jazzy-unique-identifier-msgs \
  ros-jazzy-rosidl-default-generators ros-jazzy-rosidl-default-runtime \
  ros-jazzy-ament-cmake ros-jazzy-ament-cmake-python \
  ros-jazzy-ament-cmake-pytest \
  python3-colcon-common-extensions python3-pytest
```

纯 Python 的测试工具固定在 `requirements.lock`（运行时仅用标准库，
sqlite3/hashlib/hmac/json/threading/uuid，外加 rclpy）：

```bash
python3 -m pip install -r requirements.lock   # 仅 pytest 等测试工具
```

实际验收环境：Ubuntu 24.04 / Python 3.12.3 / SQLite 3.45.1 /
rclpy 7.1.12 (jazzy)。详见 `requirements.lock`。

---

## 3. 构建

```bash
cd action_cancel_ws
source /opt/ros/jazzy/setup.bash
./build.sh                 # 增量构建（等价 colcon build --symlink-install）
# ./build.sh --clean       # 清理后全新构建
source install/setup.bash
```

---

## 4. 本地启动

需要两个终端（都先 source）：

**终端 A — 启动服务端**

```bash
cd action_cancel_ws
source /opt/ros/jazzy/setup.bash && source install/setup.bash
export ROS_LOCALHOST_ONLY=1
install/action_cancel_server/lib/action_cancel_server/segment_server \
  --db-path data/segments.db \
  --action-name segmented_task \
  --segment-delay 0.25
```

参数：

| 参数 | 含义 | 默认 |
|---|---|---|
| `--db-path` | SQLite 文件路径（自动建库；WAL） | `data/segments.db` |
| `--action-name` | action / 服务名前缀 | `segmented_task` |
| `--segment-delay` | 每段可被取消中断的节拍秒数（模拟计算耗时） | `0.05` |

**终端 B — 客户端**

```bash
cd action_cancel_ws
source /opt/ros/jazzy/setup.bash && source install/setup.bash
export ROS_LOCALHOST_ONLY=1
C=install/action_cancel_server/lib/action_cancel_server/segment_client

# 正常任务（4 段）
$C send g1 --inputs-file examples/task_normal.json --iterations 50000 --resume

# 查询单个目标的持久化记录
$C get g1

# 列出全部历史目标（--state 可按终态过滤，255=全部）
$C list --state 255
```

`send` 关键参数：`--inputs-file <json>`（字符串数组，或
`{"segment_inputs":[...]}`）、`--input`（可重复，内联一段）、
`--iterations N`（PBKDF2 轮数）、`--resume` / `--abort-on-recovery`（重启策略）、
`--cancel-after-index K`（收到第 K 段反馈后取消，用于取消末段）、
`--ignore-feedback`（丢弃全部反馈，模拟反馈丢失）。

> 注：本环境未安装 `ros2 run`（ros2cli），因此直接调用安装后的可执行文件；
> 安装了 ros2cli 的机器上也可用
> `ros2 run action_cancel_server segment_server ...`。

---

## 5. 目标生命周期与一致性设计

### 5.1 状态机（持久化于 `goals.state`）

```
                 create_goal
   (不存在) ───────────────────▶ PENDING ──mark_running──▶ RUNNING
                                   │                          │
              重启清扫（启动时）    │                          │
                                   ▼                          ▼
                              RECOVERABLE ◀────────── 重启清扫（崩溃前在跑）
                                   │
              策略 RESUME 且完整性校验通过 → RUNNING（无头续跑）
              策略 ABORT / 校验失败      → ABORTED

   RUNNING/RECOVERABLE/PENDING ──cancel 赢得 CAS──▶ CANCELED（终态）
   RUNNING/RECOVERABLE          ──末段提交赢得 CAS─▶ SUCCEEDED（终态）
   任意活跃态                    ──恢复策略/损坏──▶ ABORTED（终态）
   终态（SUCCEEDED/CANCELED/ABORTED/REJECTED）不可再迁移
```

### 5.2 取消与完成竞争如何仲裁

`storage.commit_segment()`（末段完成）与 `storage.request_cancel()`
都在 `BEGIN IMMEDIATE` 事务里执行**条件式**
`UPDATE ... WHERE state IN (RUNNING, RECOVERABLE)`。SQLite 的写锁使二者
严格串行，先拿到写锁的一方获胜：

- **完成先提交**：末段插入 + 计数 + `state=SUCCEEDED` 在同一事务原子落盘；
  随后的取消其 `WHERE` 谓词失配（目标已非活跃态），取消被拒，终态保持 SUCCEEDED。
- **取消先提交**：`state=CANCELED` 落盘；随后的末段事务检测到非活跃态，
  段记录**不插入**并回滚，返回 `rejected`，终态保持 CANCELED，段数恰为 N−1。

两种顺序下都有不变量：`completed_segments == COUNT(segments 行)`，
且终态唯一。`test_storage.py` 用两种**确定性交错**（两个独立连接）加上
**100 轮真实并发线程**验证该不变量。

### 5.3 “先持久化，后反馈”

每段先在事务中写 `segments` 行并更新计数/终态，**然后**才 `publish_feedback`。
因此反馈所声称的进度绝不会超过已落盘的进度——即使反馈全部丢失，
结果与历史仍准确（见验收 [5] 与 `test_feedback_loss_*`）。

### 5.4 去重与改参拒绝

- 目标以客户端给定的非空 `goal_id` 为主键。
- 参数（输入数组、PBKDF2 轮数、策略、ID）做规范化 SHA-256 `fingerprint`。
- 同 ID 再次提交：fingerprint 相同 → 拒绝（活跃中
  `DUPLICATE_ACTIVE`/已终结 `DUPLICATE_FINISHED`）；fingerprint 不同 →
  拒绝 `DUPLICATE_ID_CHANGED_PARAMETERS`，并在 `rejections` 表留审计记录。
  原目标结果不受影响。
- 参数校验失败（空 ID、空输入、轮数≤0、非法策略）在 action 层直接 REJECT。

### 5.5 进程中断恢复（显式策略）

1. 服务端启动执行一次性清扫：所有 `PENDING/RUNNING` → `RECOVERABLE`
   （绝不静默续跑或遗忘）。
2. 对每个 RECOVERABLE 目标读取其**自带的** `recovery_policy`：
   - `POLICY_ABORT(1)`：立即转 `ABORTED`（`ABORTED_BY_RECOVERY_POLICY`），
     已完成段保留供审计，**不**再计算。
   - `POLICY_RESUME(0)`：用存储的原始输入**重算每段** PBKDF2 与链式哈希并
     常量时间比对（`RECOVERY_INTEGRITY_ERROR` 则中止）；通过后
     `RECOVERABLE→RUNNING`，把既有段标记 `recovered=1`，由一个**无头**
     （无实时客户端）后台线程从缺口继续，直到 SUCCEEDED。
3. 段结果是幂等的：同一索引相同哈希的重投递返回 `already_committed`，
   冲突哈希抛错。

---

## 6. 接口

### Action：`segmented_task`（`SegmentedTask.action`）

- Goal：`goal_id`, `segment_inputs[]`, `pbkdf2_iterations`, `recovery_policy`
- Feedback：`goal_id`, `segment_index`, `completed_segments`,
  `segment_hash`(本段 PBKDF2), `chained_hash`(累计链)
- Result：`goal_id`, `terminal_state`, `completed_segments`,
  `total_segments`, `result_hash`, `error_code`

状态数值（result 与 history 一致）：`PENDING=0 RUNNING=1 RECOVERABLE=2
SUCCEEDED=3 CANCELED=4 ABORTED=5 REJECTED=6`。

### 服务

- `segmented_task/get_goal`（`GetGoal`）：返回目标终态、fingerprint、
  策略、result_hash、error_code 与每段 `SegmentRecord`（含 `recovered` 标志）。
- `segmented_task/list_goals`（`ListGoals`）：按状态（255=全部）列出
  `GoalSummary`。

错误码（`states.py`）：`DUPLICATE_FINISHED`、`DUPLICATE_ACTIVE`、
`DUPLICATE_RECOVERABLE`、`DUPLICATE_ID_CHANGED_PARAMETERS`、
`ABORTED_BY_RECOVERY_POLICY`、`RECOVERY_INTEGRITY_ERROR`、
`CANCELED_DURING_EXECUTION` 等。

---

## 7. 自动化测试

- **24 个测试，无需外部节点**（在一个进程内起真实 server + 真实 client，
  走真实 DDS 与真实 SQLite）：

```bash
source /opt/ros/jazzy/setup.bash && source install/setup.bash
colcon test --packages-select action_cancel_server        # 或 ./test.sh
colcon test-result --verbose
```

测试组成：

| 文件 | 覆盖 |
|---|---|
| `test_crypto.py` | PBKDF2 向量、salt 绑定 ID/索引/轮数、链序敏感、fingerprint 改参检测、常量时间比较 |
| `test_storage.py` | 去重各态、取消后拒投段结果、**取消/完成竞争的确定性交错 + 100 轮真实并发**、幂等重投递、启动清扫、RESUME/ABORT 流转、跨重开持久化 |
| `test_action_integration.py` | 真实 action 全链路：正常完成、重复目标、改参拒绝、**取消最后一段单终态**、反馈丢失、非法目标、崩溃后 RESUME 续跑、崩溃后 ABORT、list/get 历史 |

### 端到端验收（真实进程 + kill -9）

```bash
cd action_cancel_ws
source /opt/ros/jazzy/setup.bash && source install/setup.bash
./accept.sh
```

`accept.sh` 会**真实地**启动/杀掉/重启服务端进程，覆盖：

1. 正常运行终态与持久化段一致；result_hash == 末段链式哈希；
2. 同 ID 同参数重复 → 拒绝，不重跑；
3. 同 ID 改参数 → 拒绝，原 4 段完整；
4. **取消最后一段**：唯一 CANCELED，4/5 段持久化，无 result hash，
   `CancelGoal` 返回 ERROR_NONE 且 `goals_canceling` 含该目标；
5. 反馈全丢仍 SUCCEEDED 且段数准确；
6. `kill -9` + `POLICY_RESUME`：重启后 RECOVERABLE→无头续跑→SUCCEEDED，
   崩溃前段 `recovered=true`；
7. `kill -9` + `POLICY_ABORT`：重启后 RECOVERABLE→ABORTED，已完成段保留；
8. get/list 历史查询与状态过滤。

最近一次运行结果：**27 passed, 0 failed**。

---

## 8. 手动复现实验（场景速查）

```bash
# 取消“最后一段”：收到第 4 段（index 3）反馈后立即取消，第 5 段在跑
$C send gc --inputs-file examples/task_cancel_last.json --iterations 50000 \
    --resume --cancel-after-index 3
$C get gc

# 反馈丢失：客户端丢弃全部反馈，结果/历史仍精确
$C send gn --input a --input b --input c --iterations 50000 --resume --ignore-feedback

# 进程中断（策略 RESUME）：后台跑重任务，另开终端 kill -9 服务端，再重启
$C send gr --inputs-file examples/task_crash_resume.json --iterations 2000000 --resume
#   kill -9 <server_pid>
#   重新运行终端 A 的启动命令（同一 --db-path）
$C get gr     # 观察 RECOVERABLE 后变为 SUCCEEDED，前段 recovered=true
```

直接检查 SQLite：

```bash
sqlite3 data/segments.db \
  "SELECT goal_id,state,completed_segments,total_segments,error_code FROM goals;"
sqlite3 data/segments.db \
  "SELECT goal_id,segment_index,recovered,substr(chained_hash,1,12) FROM segments;"
sqlite3 data/segments.db "SELECT goal_id,reason FROM rejections;"
```

---

## 9. 设计说明与边界

- **无真实机器人**：每段“工作”是可验证的真实计算。`--segment-delay`
  只是为取消/崩溃测试提供确定的时间窗口，可设为 0。
- **密码学真实执行**：`crypto.py` 全部使用 `hashlib`/`hmac`，无桩、无随机；
  测试用已知 PBKDF2 向量与重算校验，失败会真实报错。
- **持久化可靠性**：SQLite WAL + `synchronous=FULL`，段进度在反馈前提交。
  `kill -9` 后最多损失正在计算、尚未提交的那一段（幂等可续）。
- **rcl_action 状态机配合**：取消回调只做持久化仲裁并返回
  ACCEPT/REJECT，由 rclpy 自行完成 `EXECUTING→CANCELING`；执行循环检测到
  `is_cancel_requested` 后等待进入 CANCELING 再发唯一的终态事件，避免
  Jazzy 下“EXECUTING 上直接 CANCELED”的非法迁移。
- 生产部署可在多机/多进程前加单实例约束；当前以单服务端进程 + 单写连接为
  一致性边界（跨进程写由 SQLite WAL 串行化，已在测试中以双连接验证）。
