# ROS2 相机 / IMU 消息时间对齐（纯后端）

按**事件时间**（消息头时间戳）把每帧相机图像对齐到允许误差内最近的 IMU 采样，
并用 **SQLite + 真实 SHA-256 / HMAC-SHA256 哈希链**输出不可抵赖的配对证据。
包含一个**完全无需硬件**的合成发布器，可在本机用真实 DDS 流量跑通整条链路。

- 语言/框架：Python 3.12、ROS 2 Jazzy（`rclpy`）、SQLite 3（标准库）
- 纯后端：无任何前端页面；证据以 JSON 话题 + SQLite + CLI 表格呈现
- 计算与密码操作均**真实执行**（不是 mock），测试失败会如实报告

---

## 1. 对齐规则（核心语义）

| 需求 | 实现 |
|------|------|
| 按事件时间匹配 | 使用 `header.stamp`（纳秒）；`stamp==0` 时回退到达时钟并在证据里标记 `stamp_fell_back_to_recv` |
| 最近 IMU | 在 `tolerance_ms` 内选 `|imu − camera|` 最小者 |
| 平局选较早 | 等距时选 IMU 时间戳**更早**的；时间戳完全相同则取较小 `seq`（确定性） |
| IMU 复用/独占 | 参数 `imu_policy=reuse|exclusive`；`exclusive` 时一个 IMU 只能被一帧消费 |
| 缓存有界 | `camera_cache_max` / `imu_cache_max`；超界强制结算并显式标记 `forced_eviction`，且绝不会丢弃能决定某帧的 IMU |
| 乱序容忍 | `out_of_order_ms=100`：晚到 100ms 内的数据仍参与匹配 |
| 过期显式标记 | 无法再改变结果的帧落为 `CAMERA_UNMATCHED`（原因 `expired_*`）；独占模式多余 IMU 落为 `IMU_UNMATCHED` |
| 时钟回跳 | 回跳超过 `reset_threshold_ms`（默认 100ms）开启新 **epoch**；**绝不跨 epoch 配对** |
| 参数原子生效 | 参数变更先 *stage*，在下一个消息**边界**原子应用；每次应用得到单调递增版本号，盖在该版本下结算的每条决策上 |

`dt_ns` 约定为 **camera − imu**（相机早于 IMU 为负）。

---

## 2. 目录结构

```
.
├── src/
│   ├── time_alignment/                 # 不依赖 rclpy 的核心（可纯离线测试）
│   │   ├── config.py                   #   不可变、带版本的配置
│   │   ├── types.py                    #   Event / Decision / 状态码
│   │   ├── engine.py                   #   对齐状态机（匹配/缓存/epoch/结算）
│   │   ├── storage.py                  #   SQLite + 哈希/HMAC 链 + 篡改校验
│   │   ├── scenario.py                 #   离线 JSON 场景回放 + 合成场景
│   │   └── cli.py                      #   replay / gen-scenarios / verify
│   └── time_alignment_nodes/           # ROS2 节点（rclpy）
│       ├── aligner_node.py             #   订阅 Image/Imu，落库，发证据 JSON
│       ├── synthetic_publisher.py      #   无硬件合成发布器（突发/丢包/重复/重置）
│       ├── main.py / run_*.py          #   节点入口
├── examples/                           # 6 个内置示例场景 JSON（含期望）
├── tests/                              # 40 个自动化测试（含真实 DDS 端到端）
├── package.xml / setup.py / setup.cfg  # ament_python 包
├── requirements.txt / requirements-lock.txt
└── README.md
```

---

## 3. 安装

需要 Ubuntu 24.04 + ROS 2 Jazzy（本机已装在 `/opt/ros/jazzy`）。

```bash
# 3.1 ROS 环境（每个新终端都要 source）
source /opt/ros/jazzy/setup.bash

# 3.2 （可选）离线测试/CLI 依赖，核心运行时只用标准库
python3 -m pip install -r requirements-lock.txt

# 3.3 直接以源码方式运行（已在仓库根目录，把 src 加入 PYTHONPATH）
export PYTHONPATH="$PWD/src:/opt/ros/jazzy/lib/python3.12/site-packages"

# 3.4 （可选）作为 ament 包安装，之后可 ros2 run
colcon build --packages-select time_alignment && source install/setup.bash
```

> 不要 `pip install rclpy`：PyPI 上没有该包，它由 ROS 的 apt 包提供。

---

## 4. 快速验收（不启动 ROS，最快路径）

```bash
source /opt/ros/jazzy/setup.bash
export PYTHONPATH="$PWD/src:/opt/ros/jazzy/lib/python3.12/site-packages"

# 4.1 生成/查看示例场景（已随仓库提供于 examples/）
python3 -m time_alignment.cli gen-scenarios examples

# 4.2 回放"丢包"场景，打印配对证据并落库，校验期望值与哈希链
python3 -m time_alignment.cli replay examples/drops.json --db /tmp/drops.sqlite

# 4.3 离线校验数据库哈希链
python3 -m time_alignment.cli verify /tmp/drops.sqlite
# 期望输出: CHAIN OK: ... rows, SHA256, tail=...
```

其它场景：`burst`（突发）、`identical_timestamps`（相同时间戳/平局）、
`clock_reset`（时钟重置/多 epoch）、`out_of_order`（乱序迟到）、
`loose_tolerance`（宽容差）。

---

## 5. 真实 ROS2 双进程本地启动（合成发布器 + 对齐节点）

打开两个终端，各自先：

```bash
source /opt/ros/jazzy/setup.bash
cd <仓库根目录>
export PYTHONPATH="$PWD/src:/opt/ros/jazzy/lib/python3.12/site-packages"
export ROS_DOMAIN_ID=88 ROS_LOCALHOST_ONLY=1     # 隔离到本机、独立域
```

**终端 A — 对齐节点：**

```bash
export TIME_ALIGNMENT_HMAC_SECRET="demo-secret"   # 设置后启用 HMAC 认证
rm -f /tmp/accept.sqlite
python3 -m time_alignment_nodes.run_aligner --ros-args \
  -p db_path:=/tmp/accept.sqlite \
  -p imu_policy:=exclusive -p tolerance_ms:=5.0 \
  -p camera_topic:=acc/image -p imu_topic:=acc/imu
```

**终端 B — 合成发布器（无硬件，选一种 `mode`）：**

```bash
# mode 可选: steady | burst | drops | identical | reset
python3 -m time_alignment_nodes.run_publisher --ros-args \
  -p mode:=drops -p duration_s:=3.0 -p camera_hz:=10.0 -p imu_hz:=100.0 \
  -p camera_topic:=acc/image -p imu_topic:=acc/imu
```

**终端 C — 实时看配对证据（JSON，std_msgs/String）：**

```bash
ros2 topic echo /alignment/evidence
```

发布器跑完后，在终端 A 按 `Ctrl+C`，节点会排空剩余数据并打印
`finalized: N decisions, HMAC-SHA256 chain verified OK`。

**带密钥校验：**

```bash
TIME_ALIGNMENT_HMAC_SECRET="demo-secret" \
  python3 -m time_alignment.cli verify /tmp/accept.sqlite \
  --hmac-env TIME_ALIGNMENT_HMAC_SECRET
```

用错密钥或改动任意一行都会得到非零退出码与 `TAMPERED/INVALID`（已在测试中验证）。

**运行时动态改参数（边界原子生效，自动升版本）：**

```bash
ros2 param set /ta_aligner tolerance_ms 25.0     # 下条消息边界生效 -> config v2
ros2 param set /ta_aligner imu_policy reuse
ros2 param set /ta_aligner tolerance_ms -1       # 被拒绝（校验失败）
```

---

## 6. 自动化测试

```bash
source /opt/ros/jazzy/setup.bash
# 不 source ROS 时，rclpy 用例自动 skip，离线用例照常运行
python3 -m pytest tests/ -q
```

| 测试文件 | 内容 |
|----------|------|
| `tests/test_engine.py` | 最近匹配、等距/同戳平局、独占/复用、乱序、epoch、有界缓存、参数原子版本 |
| `tests/test_storage_crypto.py` | SHA-256/HMAC 链、篡改/删除/乱序检测、错密钥失败、独占不变量 |
| `tests/test_scenarios_cli.py` | 6 个场景期望、CLI 成功/失败、落库与证据报告 |
| `tests/test_ros_nodes.py` | **真实 rclpy 节点 + DDS**：合成发布→对齐→SQLite，动态参数更新 |

已在本环境实测：**40 passed**（含 5 个真实 DDS 在线用例）。

一键复跑全部内置场景（等价验收脚本）：

```bash
python3 - <<'PY'
from time_alignment.scenario import GENERATORS, run_scenario, check_expectations
for name, gen in GENERATORS.items():
    sc = gen(); s = run_scenario(sc)
    print(f"{name:24s}", "PASS" if not check_expectations(s, sc) else "FAIL",
          s["status_counts"], "chain_ok=", s["chain"]["ok"])
PY
```

---

## 7. 证据字段说明（SQLite `decisions` 表 / JSON）

- `status`：`MATCHED` / `CAMERA_UNMATCHED` / `IMU_UNMATCHED`
- `epoch_id`：epoch（时钟重置递增），跨 epoch 永不配对
- `config_version`：决策结算时生效的参数版本（边界原子）
- `camera_seq/camera_stamp_ns`、`imu_seq/imu_stamp_ns`、`dt_ns/abs_dt_ns`
- `tie` + `reason`：是否平局及依据（`nearest_within_tolerance` /
  `tie_equidistant_earlier_stamp` / `tie_identical_stamp_lowest_seq` /
  `expired_no_imu_within_tolerance` / `epoch_closed_without_match` /
  `cache_bound_forced_eviction` / `imu_expired_without_camera` …）
- `candidates`：最近 5 个候选 IMU 及其距离/哈希（完整"为什么是它"证据）
- `camera_payload_hash` / `imu_payload_hash`：对**真实消息字节**的 SHA-256
  （图像对几何字段+像素字节；IMU 对二进制字段）
- `prev_hash`/`row_hash`：哈希链；`config_versions`、`epochs` 表分别记录
  参数版本与 epoch 生命周期

---

## 8. 设计要点

- **核心不依赖 rclpy**：`engine.py` 只吃 `Event`、吐 `Decision`，因此在线节点与
  离线回放/测试跑的是同一份对齐逻辑，杜绝两套实现漂移。
- **结算水位**：一帧只有当两路 high-water 都推进到 `stamp + tolerance +
  out_of_order` 之后才结算，保证 100ms 迟到数据不会错过匹配。
- **IMU 过期保守化**：只有当一个 IMU 对所有"仍可能到达/仍挂起"的相机都不可能
  落在容差内时才丢弃，避免提前判死。
- **哈希链**：`row_hash = H(prev_hash || canonical_json(行字段))`，HMAC 用真实
  `hmac` 库常量时间比较；删除、重排、改任意字段、错密钥都会被定位到具体行。

## 9. 已知限制

- 单写入者（节点内部串行回调/单执行器）；多进程写同一 SQLite 未做。
- 图像/IMU 使用 BEST_EFFORT QoS（适配典型传感器流）。
- 话题名、DB 路径等静态接线参数修改后需重启节点生效（对齐行为参数可热更新）。
