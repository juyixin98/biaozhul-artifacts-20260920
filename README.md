# 动作取消一致性（Action Cancellation Consistency）

一个纯后端 ROS 2 演示：执行「合成分段任务」的 action server。每个段执行**真实的**
链式 SHA-256 计算（不控制真实机器人），每段发布反馈并把进度**持久化到 SQLite**，
重点保证以下一致性性质：

1. **目标 ID 去重**：`goal_id` 是客户端指定的幂等键，重复 ID 由 SQLite 主键
   原子去重；同 ID **不同参数一律拒绝**；同 ID 同参数在目标已结束后重放，
   返回存储的同一终态结果（`code=REPLAYED`），不会重新计算。
2. **取消与完成竞争只产生一个终态**：所有状态迁移都是 SQLite 条件 UPDATE
   （CAS）。取消请求（`CANCELING`）与最后一段提交同时发生时，只有一个 CAS
   生效，数据库里**有且仅有一个终态**（策略：已记录的取消请求优先 → `CANCELED`；
   无取消且全部完成 → `SUCCEEDED`）。
3. **已取消目标不能再提交段结果**：段提交事务要求状态必须是 `RUNNING/RECOVERING`
   且段序号连续。即使工作线程正在最后一段的计算循环里，取消落地后其结果提交
   会被数据库拒绝。
4. **崩溃/进程中断后恢复**：启动时把上一进程遗留的非终态目标标记为
   `RECOVERING`，并按**明确策略**处理：
   - 节点级 `recovery_mode`：`resume`（默认）/ `abort` / `manual`（挂起，
     等待管理服务指令）；
   - 每个目标自带 `policy`：`resume` 从未提交段继续（输入是链式的，重算
     结果逐位一致）；`abort` 重启后立即置 `ABORTED`，保留已完成段计数；
   - 已记录的取消请求跨重启不丢失（`cancel_requested` 粘滞位）。
5. **历史可查询**：`query_goal` / `list_goals` 服务在客户端断开、服务重启后
   仍可查询任意历史目标的终态、完成段数、结果哈希。
6. **反馈是建议性的**：可按概率故意丢弃反馈消息；丢失反馈不改变任何结果，
   SQLite 与 action result 才是事实来源。

此外提供**密码学完整性**：每次状态迁移在同一事务内向追加式事件表写入一条
`HMAC-SHA256` 哈希链事件；`segtask_audit` 工具重放全链、检测事件篡改/删除，
并用全新 SHA-256 计算**独立重算每个段输出**以核验结果确实被真正计算过。

---

## 目录结构

```
src/
  segtask_msgs/                 # 自定义 action / service / msg 接口
    action/SegmentTask.action
    msg/GoalInfo.msg
    srv/QueryGoal.srv  srv/ListGoals.srv  srv/AdminRequest.srv
  segtask_server/
    segtask_server/
      status.py                 # 状态码 / 结果码 / 策略常量
      crypto.py                 # 真实 SHA-256 段计算 + HMAC 哈希链 + 密钥管理
      db.py                     # SQLite：CAS 状态机、段提交防护、恢复、事件链
      runner.py                 # 段执行循环（检查点取消、反馈发布/丢弃）
      node.py                   # rclpy action server + 查询/管理服务 + 启动恢复
      audit.py                  # 链校验 + 段重算的 CLI/库
      clients/client_lib.py     # 阻塞式测试客户端
    scripts/                    # 4 个场景验收脚本 + 示例提交脚本
    examples/goals.json         # 示例输入
tests/                          # pytest：22 个单元测试 + 9 个端到端测试
requirements.lock.txt
```

ROS 接口（绝对名称）：

| 类型 | 名称 |
|---|---|
| action | `/segtask_server/segment_task` (`segtask_msgs/SegmentTask`) |
| service | `/segtask_server/query_goal` (`QueryGoal`) |
| service | `/segtask_server/list_goals` (`ListGoals`) |
| service | `/segtask_server/admin` (`AdminRequest`, `recover`/`abort`) |

---

## 环境要求

- Ubuntu 24.04 + ROS 2 **Jazzy**（已验证：`ros-jazzy-rclpy 7.1.12`、
  `ros-jazzy-action-msgs 2.0.4`、`ros-jazzy-rosidl-generator-py 0.22.2`）
- Python 3.12、colcon、pytest（`sqlite3`/`hashlib`/`hmac` 为标准库）

无额外 PyPI 运行时依赖（rclpy 由 ROS 系统包提供，**不能** pip 安装）：

```bash
python3 -m pip install -r requirements.lock.txt   # 仅 pytest==9.1.1
```

---

## 本地启动

```bash
cd ~/Downloads/biaozhul/P044/a
source /opt/ros/jazzy/setup.bash

# 1) 构建（先接口包后 Python 包）
colcon build --symlink-install --packages-select segtask_msgs
source install/setup.bash
colcon build --symlink-install --packages-select segtask_server
source install/setup.bash

# 2) 启动 action server（默认把数据写到 ./segtask_data/segtask.db）
#    标准方式：
ros2 run segtask_server segtask_server_node --ros-args -p recovery_mode:=resume
#    若本机 ros2 CLI 缺少 run 子命令，直接使用入口脚本等价：
./install/segtask_server/lib/segtask_server/segtask_server_node \
    --ros-args -p db_path:=/tmp/segtask/run.db -p recovery_mode:=resume
```

参数：

| 参数 | 默认 | 说明 |
|---|---|---|
| `db_path` | `segtask_data/segtask.db` | SQLite 路径；同目录生成 0600 权限的 `.key` |
| `recovery_mode` | `resume` | `resume` / `abort` / `manual` |
| `feedback_drop_rate` | `0.0` | 故意丢弃反馈的概率，范围 [0,1] |
| `feedback_drop_seed` | `-1` | 丢弃随机种子，-1 表示真随机 |

提交示例输入（另开终端）：

```bash
source /opt/ros/jazzy/setup.bash && source install/setup.bash
./install/segtask_server/lib/segtask_server/submit_examples.py
```

审计（链校验 + 段重算，离线可跑，退出码非 0 表示发现问题）：

```bash
./install/segtask_server/lib/segtask_server/segtask_audit \
    --db-path /tmp/segtask/run.db
```

---

## 验收命令

### A. 自动化测试（推荐入口）

```bash
source /opt/ros/jazzy/setup.bash && source install/setup.bash
python3 -m pytest tests/ -v
# 22 个纯单元测试（crypto/db，无需 ROS 守护进程）
#  9 个端到端测试（自动拉起/杀掉真实 server 子进程，使用独立 ROS_DOMAIN_ID）
```

仅单元测试：`python3 -m pytest tests/test_crypto.py tests/test_db.py -v`

端到端测试会自动在 `~/Downloads/biaozhul/P044/a/install/...` 查找 server
二进制；其它安装位置可用环境变量覆盖：`SEGTASK_NODE_BIN`、`SEGTASK_AUDIT_BIN`。

### B. 四个场景脚本（每个脚本自行管理 server 生命周期，3/4 会真的 SIGKILL 进程）

脚本 1/2 需要一个已在运行的 server；脚本 3/4 自己拉起 server。

```bash
source /opt/ros/jazzy/setup.bash && source install/setup.bash
export ROS_DOMAIN_ID=88 ROS_LOCALHOST_ONLY=1

# 场景 1：重复目标（活动重复拒绝、异参拒绝、终态同参重放、历史查询）
./install/segtask_server/lib/segtask_server/segtask_server_node \
    --ros-args -p db_path:=/tmp/acc.db >/tmp/acc.log 2>&1 &
python3 src/segtask_server/scripts/01_test_duplicate_goal.py

# 场景 2：取消最后一段（唯一终态、计数与持久行一致、终态后计数冻结）
python3 src/segtask_server/scripts/02_test_cancel_last_segment.py

# 场景 3：SIGKILL 进程中断 → 重启（resume 续跑成功 / abort 中止保段 + 离线审计）
python3 src/segtask_server/scripts/03_test_process_crash.py

# 场景 4：60%~70% 反馈丢失（目标仍成功；另一目标在丢反馈下仍可靠取消）
python3 src/segtask_server/scripts/04_test_feedback_loss.py
```

各脚本逐条打印 `[PASS]/[FAIL]`，全部通过时退出码为 0。

### C. 预期一致性判据（脚本实际断言的内容）

- 终态唯一：QueryGoal.status 与 action result.status 相同；终态后再等待，
  `segments_done` 不再变化；事件序列里 `GOAL_*` 终态事件恰好一个。
- 终态与实际完成段一致：`goals.segments_done` == `segments` 表行数 ==
  action result 的 `segments_done`；取消发生在最后一段时，该段输出可能
  恰好已提交也可能没提交（这正是竞争），但三个口径必然一致。
- 崩溃现场：SIGKILL 后磁盘上状态是 RUNNING、进度在 1..N-1；重启后
  resume 目标最终 SUCCEEDED 且总段数齐全，abort 目标 ABORTED 且部分段保留，
  `resumed_after_restart=1`，审计通过。

---

## 一致性机制如何落地（关键设计）

- **去重**：`INSERT ... PRIMARY KEY` 在 `BEGIN IMMEDIATE` 事务内完成
  insert-or-fetch；参数以 canonical JSON 的 SHA-256（`params_hash`）比较，
  同 ID 异参在 action `goal_callback` 阶段拒绝。
- **竞争仲裁**：`request_cancel` 与 `finalize` 都是
  `UPDATE ... WHERE status IN (...)` 形式的 CAS，行级写锁串行化，两个并发
  终态化调用中只有一个 `rowcount==1`；另一个读到的是已终态行。
- **段提交防护**：状态守卫 + 序号守卫 + 段插入 + 计数更新 + 事件写入在同一
  事务；CANCELED/CANCELING 下任何提交（含迟到结果）都回滚拒绝。
- **崩溃恢复**：重启事务把所有非终态行批量置 RECOVERING 并写 `RECOVERY_MARKED`
  事件；`resume` 用已提交段的输出哈希作为下一段种子，因 SHA-256 链是确定的，
  续跑结果与未崩溃时完全一致（审计重算证明）。
- **密码操作真实执行**：段工作是 `work_units` 次真实 SHA-256 迭代；链是真实
  HMAC-SHA256（`hmac.compare_digest` 常数时间比较）；密钥 32 字节 `os.urandom`，
  0600 文件保存。无任何桩实现；失败会以非零退出码/异常如实暴露。

## 说明与限制

- 纯后端：没有任何前端页面。
- 恢复续跑在服务端后台线程执行，使用 SQLite 而非 ROS 反馈通道；恢复期间
  没有 ROS action 句柄（原客户端已随崩溃消失），进度通过 QueryGoal 观察，
  这是刻意设计——恢复事实以数据库为准。
- 段计算为 CPU 密集型，`work_units` 越大单段越久；验收脚本默认
  10万–500万次哈希迭代，可通过 `--work-units` 调整。
