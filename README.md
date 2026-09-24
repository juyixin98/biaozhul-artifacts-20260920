# ROS2 相机 / IMU 消息时间对齐（纯后端）

按 **事件时间（消息头时间戳）** 把每帧相机数据与允许误差内最近的 IMU 采样对齐，
所有判定结果写入 **SQLite 追加式证据表**，每行带 **SHA-256 摘要 + HMAC-SHA256
哈希链**（标准库 `hashlib`/`hmac` 真实密码学运算，可离线验真、可检出任何篡改）。
另提供 **无需任何硬件** 的合成发布器与场景回放器。

纯 Python 核心（算法、存储、CLI、测试）**零第三方依赖、不要求安装 ROS**；
rclpy 节点在装有 ROS2 的机器上可直接 `ros2 run`。

---

## 1. 目录结构

```
time_alignment/
  model.py        # 数据模型、参数快照(带版本)、状态枚举
  matcher.py      # 对齐引擎：水印、乱序、epoch、复用/独占、有界缓存
  storage.py      # SQLite + SHA-256/HMAC-SHA256 证据链、验真
  scenario.py     # 场景 JSON 加载、确定性回放、期望断言
  cli.py          # replay / verify / show / genkey（无硬件入口）
  node.py         # rclpy 对齐节点（惰性 import rclpy；核心可脱离 ROS 测试）
  synthetic.py    # rclpy 合成发布器 + 可单测的假消息工厂
examples/         # 突发/同时间戳/丢包/乱序/时钟重置/缓存溢出/参数边界
tests/            # 41 个自动化测试（引擎/密码学/场景/节点适配层）
scripts/acceptance.sh   # 一键端到端验收
package.xml setup.py setup.cfg pyproject.toml  # ROS2 ament_python + pip 打包
```

## 2. 对齐语义（关键决策）

| 主题 | 规则 |
|---|---|
| 时间基准 | **事件时间** = 消息头 `header.stamp`；接收顺序只决定处理顺序 |
| 匹配目标 | 最小化 `|t_cam − t_imu|`，且必须 `≤ tolerance_ns`（默认 5 ms） |
| 平局规则 | 距离相同选 **事件时间更早** 的 IMU；时间戳再相同按接收序号，保证确定性 |
| IMU 独占 | `imu_exclusive=true`（默认）：每个 IMU 至多被一帧消费 |
| IMU 复用 | `imu_exclusive=false` + `imu_max_uses`（默认 1，`-1` 不限） |
| 乱序容忍 | `reorder_tolerance_ns=100000000`（100 ms）。水印 `W = epoch内最大事件时间 − 100ms`；`t≤W` 的帧才定稿 |
| 过期未配对 | 水印到期仍无候选 → 明确写 `expired_unpaired`；流结束写 `stream_end_unpaired` |
| 未用 IMU | 不可能再被匹配时显式写 `unused_expired`（区分 `used_expired`） |
| 有界缓存 | 相机/IMU 待配对缓存均有上限；溢出淘汰最旧项并写 `*_buffer_overflow` |
| 时间回跳 | `t + 100ms < epoch最大事件时间` 即判定时钟回跳，**开新 epoch**；或收到显式 reset / `/clock` epoch 变化 |
| 不跨 epoch | epoch 关闭时冲刷全部待配对项（`epoch_closed_unpaired` 等），绝不与另一 epoch 数据配对 |
| 参数生效 | 参数回调只入队并立即校验；在接收边界 **原子换入**，单调递增 `version`；每条证据记录其生效版本 |

`dt_ns` 约定：正 = IMU 早于相机；证据中 `tie_break="earlier"` 表示存在等距但更晚的候选被主动舍弃。

## 3. 证据链（防篡改）

每条记录：

```
body   = seq | kind | epoch | params_version | canonical_json(payload)
digest = SHA256(body)
mac_n  = HMAC-SHA256(key, mac_{n-1} || digest)     # 第 0 行前缀为空
```

篡改任意字段、重排、删除行、用错密钥，`verify` 都会失败并定位首处错误。
SQLite 开启 WAL + `synchronous=FULL`。

## 4. 环境要求

- Python ≥ 3.10（开发机验证：3.12.3）；标准库自带 SQLite（验证：3.45.1）
- 运行/验收核心功能 **无需 pip 安装任何包**
- 测试：`pip install -r requirements-dev.txt`（锁定 `pytest==9.1.1`）
- rclpy 节点另需 ROS2（humble/iron/jazzy/rolling），**不** 走 pip，见下

## 5. 本地启动与验收（无硬件、无 ROS）

```bash
cd P041/b

# (可选) 安装唯一的开发依赖
python3 -m pip install -r requirements-dev.txt

# 一键验收：测试 + 全部场景落库 + HMAC 验真 + 篡改必检出
bash scripts/acceptance.sh
```

单独运行：

```bash
# 生成 32 字节随机 HMAC 密钥（secrets=os.urandom，文件权限 0600）
python3 -m time_alignment.cli genkey --out build/hmac.key

# 回放一个场景：打印配对证据，并写入 SQLite + JSONL
python3 -m time_alignment.cli replay examples/burst.json \
    --db build/burst.db --key build/hmac.key --export build/burst.jsonl

# 验真
python3 -m time_alignment.cli verify build/burst.db --key build/hmac.key

# 逐行查看
python3 -m time_alignment.cli show build/burst.db

# 只回放、只跑断言（不落库）
python3 -m time_alignment.cli replay examples/clock_reset.json
```

`replay` 在任一 `expect` 断言不满足时以非零退出；`verify` 链无效时以非零退出。

### 示例场景（对应需求中的测试点）

| 文件 | 覆盖 |
|---|---|
| `examples/burst.json` | 突发同刻多帧、等距平局选更早、100ms 内迟到仍可配、丢包 `expired_unpaired` |
| `examples/same_timestamp_exclusive.json` | 完全相同时间戳：独占模式前两帧成功、第三帧明确未配对 |
| `examples/unordered.json` | 49ms 迟到被接受；超过 100ms 的迟到触发新 epoch |
| `examples/clock_reset.json` | 显式 reset + 时间回跳两个新 epoch；epoch 间绝不互配 |
| `examples/params_boundary.json` | v1 独占配对先定稿，边界后 v2 无限复用；记录分别带版本 |
| `examples/overflow.json` | 有界 IMU 缓存淘汰最旧项并输出 overflow 证据 |

场景 JSON 事件可用秒（`"t": 0.012`）或纳秒（`"t_ns"`），可用
`"recv"`/`"recv_ns"` 指定接收时刻构造乱序，`{"kind":"reset"}` 触发重置，
`{"kind":"params","version":2,"params":{...}}` 在该接收点排队参数变更。

### 配对证据样例（JSONL）

```json
{"seq":2,"kind":"pair","epoch":1,"params_version":1,
 "payload":{"camera":{"id":"c0","t_ns":11000000,...},
            "imu":{"id":"i0","t_ns":10000000,...},
            "dt_ns":1000000,"tie_break":"earlier",
            "status":"matched","reason":"nearest_within_tolerance",
            "watermark_ns":65000000,"imu_use_count":1,
            "epoch":1,"params_version":1},
 "digest":"9bbdeaee…","mac":"69861c4e…","created_ns":1790135845222260385}
```

## 6. ROS2 实机启动（有 ROS 时）

```bash
# 依赖（示例为 jazzy，按实际发行版替换）
sudo apt install ros-jazzy-rclpy ros-jazzy-sensor-msgs \
                 ros-jazzy-std-msgs ros-jazzy-rcl-interfaces
source /opt/ros/jazzy/setup.bash

# 在工作区中编译
cd P041/b
colcon build --packages-select time_alignment
source install/setup.bash

# 终端 A：对齐节点（声明参数 + 落 SQLite）
ros2 run time_alignment aligner_node --ros-args -r __ns:=/sense \
    -p tolerance_ns:=5000000 -p reorder_tolerance_ns:=100000000 \
    -p imu_exclusive:=true

# 终端 B：无硬件合成发布器（内置名义流，或回放场景）
ros2 run time_alignment synthetic_publisher
ros2 run time_alignment synthetic_publisher --ros-args \
    -p scenario:=examples/clock_reset.json

# 运行期改参（边界原子生效，被拒绝的非法值返回 unsuccessful）
ros2 param set /time_aligner imu_exclusive false
ros2 param set /time_aligner imu_max_uses -1
```

话题：`camera/image`（sensor_msgs/Image，BEST_EFFORT QoS）、
`imu/data`（sensor_msgs/Imu）、`clock/reset`（std_msgs/Empty，宣告时钟重置）。
节点优雅退出时冲刷残余缓存并自检证据链。

> 当前验收环境 **未安装 ROS2/rclpy**（已实测 `import rclpy` 失败），因此
> 第 6 节的 `ros2 run` 未在此环境执行；节点回调路径由
> `tests/test_node_glue.py` 用鸭子类型假消息端到端覆盖（转换→匹配→落库→参数
> 回调→reset），rclpy 缺失时 `create_aligner_node()` 会打印安装指引并退出。
> 无 ROS 的全部功能（第 5 节）均已真实执行并通过。

## 7. 测试

```bash
python3 -m pytest tests/ -q          # 41 passed
```

- `test_matcher.py`：最近邻/平局/独占/复用（含上限）/乱序/回跳/reset/有界缓存/参数版本与原子性
- `test_storage.py`：往返、载荷篡改、MAC 交换、错密钥、删行检测、独立重算 HMAC、JSONL、断点续写
- `test_scenarios.py`：全部示例场景的期望断言 + CLI 落库验真
- `test_node_glue.py`：无 rclpy 的节点回调端到端、假消息工厂、参数拒绝

## 8. 设计说明

- 匹配引擎（`matcher.py`）不依赖存储，便于单测；`AlignerCore` 负责引擎与证据库接线。
- 每帧只在定稿时产生记录；IMU 在“不可能再服务任何在窗相机”（`t_imu < W − tol`）时退休。
- 同 epoch 内相机按事件时间依次解析，配合“独占即摘除”得到稳定且无饿死的贪心配对。
