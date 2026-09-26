# 惯性数据预积分子集（IMU Pre-integration Subset）

纯后端离线计算库：在**已知重力向量**与**固定陀螺/加计偏置**的前提下，对合成 IMU
数据做四元数姿态更新与速度/位置积分，通过 JSON 文件/标准输入作为入口。

- 语言/依赖：Python ≥ 3.10，仅依赖 NumPy（测试用 pytest）
- 数据来源：**全部为合成轨迹与传感器数据**，不连接任何硬件，不读 bag/设备
- 不做前端、不做可视化、不依赖 ROS
- **明确不做**：完整 SLAM、回环、外参标定、偏置在线估计（偏置由调用方给定，全程固定）

## 目录结构

```
imu_integrator/
  __init__.py       # 包入口
  quaternion.py     # [w,x,y,z] Hamilton 四元数工具
  integrator.py     # 离线积分主算法（姿态/速度/位置）
  validation.py     # 样本校验：缺样、重复时间戳、乱序、角速度单位错误等
  synthetic.py      # 合成数据：静止 / 匀速转动 / 恒加速度（支持变 dt、叠加偏置）
  cli.py            # JSON 入口（process_request）与命令行
examples/           # 请求样例（成功与错误场景）
tests/              # 自动化测试（62 个用例）
```

## 数学模型与约定

- 四元数顺序 `[w, x, y, z]`，Hamilton 约定；`q` 表示**机体系 → 世界系**旋转。
- 时间戳 `t` 单位秒；角速度 `gyro` 默认 rad/s（可声明 deg/s 自动换算）；
  加速度 `accel` 单位 m/s²，为加速度计原始比力（静止水平时读数为 `[0,0,9.81]`）。
- `gravity` 是世界系中的**重力加速度向量**，默认 `[0,0,-9.81]`。
- 局部平面世界系，不补偿地球自转与运输率。

对第 k 个采样区间（左端样本，区间长度 `dt = t[k+1]-t[k]`，允许可变）：

```
ω = gyro[k] - b_g                         # 机体系角速率
f = accel[k] - b_a                        # 机体系比力
q_{k+1} = q_k ⊗ exp(ω·dt/2)               # 右乘增量四元数
q_mid   = q_k ⊗ exp(ω·dt/4)               # 区间中点姿态
a_world = R(q_mid)·f + gravity            # 世界系线加速度
v_{k+1} = v_k + a_world·dt
p_{k+1} = p_k + v_k·dt + ½·a_world·dt²
```

姿态更新为精确的指数映射（非常值小角近似）；平移采用区间中点姿态旋转比力。
合成数据在每个区间上为左端点常值，与该分段常值假设一致，因此可用解析真值核验。

**范围限制**：偏置恒定且不随时间更新；不建模标度因子、非正交安装误差、噪声
（合成数据是无噪的）、圆锥/划摇高阶效应补偿。

## 安装与运行

无需安装即可在仓库根目录直接运行（需要 numpy）：

```bash
python3 -m pytest -q                      # 运行测试
python3 -m imu_integrator.cli --help
```

或以可编辑方式安装（提供 `imu-integrator` 命令）：

```bash
pip install -e .
```

### 命令行

```bash
# 内置合成场景演示（不依赖任何文件）
python3 -m imu_integrator.cli demo --scenario stationary
python3 -m imu_integrator.cli demo --scenario rotation
python3 -m imu_integrator.cli demo --scenario acceleration

# 从 JSON 文件执行积分（-o 写文件，默认打印到标准输出）
python3 -m imu_integrator.cli run examples/request_rotation.json -o /tmp/out.json

# 从标准输入
cat examples/request_inline.json | python3 -m imu_integrator.cli run -

# 只校验数据，不积分
python3 -m imu_integrator.cli validate examples/error_missing_samples.json
```

退出码：`0` 成功；`1` 请求/数据错误（响应体含错误码）；`2` 文件读取或 JSON 解析失败。

## JSON 请求格式

顶层字段（除 `samples`/`scenario` 外均可选）：

| 字段 | 说明 |
|---|---|
| `action` | `"integrate"`（默认）或 `"validate"` |
| `samples` | 内联样本数组：`{"t": 秒, "gyro": [x,y,z], "accel": [x,y,z]}` |
| `scenario` | 不提供 samples 时，由合成器生成：`name` 为 `stationary`/`rotation`/`acceleration` |
| `gravity` | 重力加速度向量，默认 `[0,0,-9.81]` |
| `gyro_bias` / `accel_bias` | 固定偏置，积分时从读数中扣除 |
| `initial_position` / `initial_velocity` | 初始位置 (m) / 速度 (m/s) |
| `initial_orientation` | 初始四元数 `[w,x,y,z]`，默认单位四元数 |
| `options.gyro_unit` | `"rad/s"`（默认）或 `"deg/s"`（换算后积分） |
| `options.max_dt` | 相邻样本最大允许间隔（秒），超过报 `missing_samples` |
| `options.sort` | 时间倒序时自动排序（默认 false，直接报错） |
| `options.drop_duplicates` | 丢弃重复时间戳样本，保留最先一条（默认 false，报错） |
| `options.gyro_limit_rad_s` | 角速度量程上限，默认 35 rad/s（≈2000 dps） |
| `include_trajectory` | 是否输出逐样本轨迹，默认 true |

成功响应：`{"ok": true, "result": {"summary": {...}, "trajectory": [...]}}`。
`summary` 含样本数、区间数、时长、末位置/末速度/末姿态（姿态字段 `xyzw` 为
x,y,z,w 顺序）、路径长度。失败响应：

```json
{"ok": false, "error": {"code": "duplicate_timestamp", "message": "…", "index": 2}}
```

错误码：`empty_data`、`missing_field`、`invalid_field`、`non_finite`、
`duplicate_timestamp`、`non_monotonic_time`、`missing_samples`、`gyro_unit_error`、
`invalid_unit`、`invalid_request`、`io_error`。

### 作为库调用

```python
from imu_integrator import integrate_imu, stationary_samples

samples = stationary_samples(duration=1.0, dt=0.01,
                             gyro_bias=[0.01, 0, 0])
result = integrate_imu(samples, gyro_bias=[0.01, 0, 0])
print(result.final_position, result.final_orientation)
```

## 请求样例

| 文件 | 场景 |
|---|---|
| `examples/request_stationary.json` | 静止 + 已知陀螺偏置补偿 |
| `examples/request_rotation.json` | 绕 z 轴匀速转动 2 s（0.5 rad/s） |
| `examples/request_acceleration.json` | x 向恒加速度 2 m/s²，非均匀间隔 [0.1,0.2,0.3,0.4] s |
| `examples/request_inline.json` | 内联样本（变间隔，带 max_dt 检查） |
| `examples/request_gyro_deg_s.json` | 角速度以 deg/s 给出，声明单位后积分 |
| `examples/error_duplicate_timestamp.json` | 重复时间戳 → `duplicate_timestamp`（exit 1） |
| `examples/error_missing_samples.json` | 掉帧 0.29 s > max_dt 0.05 → `missing_samples`（exit 1） |
| `examples/error_gyro_unit.json` | 500 rad/s 超量程（典型 deg/s 误标）→ `gyro_unit_error` |

## 实际运行记录（验收核验）

环境：Python 3.12.3、NumPy 2.5.3、pytest 9.1.1，Linux x86_64。

### 三个核验场景的解析真值对照

- **静止**（1.0 s，0.1 s 间隔，陀螺偏置 [0.01,-0.02,0] 并补偿）：
  末位置/末速度 `[0,0,0]`，姿态保持单位四元数。✅
- **匀速转动**（绕 z 轴 0.5 rad/s × 2 s = 1.0 rad）：
  末姿态 `xyzw = [0, 0, 0.479426, 0.877583]`，与 sin(0.5)/cos(0.5) 一致；
  绕天向轴转动不改变重力方向，位置保持 0。✅
- **恒加速度**（世界系 x 向 2 m/s²，变间隔合计 1.0 s）：
  末速度 `[2,0,0]` m/s、末位置 `[1,0,0]` m，与 v=at、p=½at² 一致。✅
- **内联变间隔样例**（加速度 [1,0,0] m/s²，间隔 0.10/0.15/0.20 s）：
  末速度 `[0.45,0,0]`、末位置 `[0.10125,0,0]`（=Σ v_k·dt+½a·dt²）。✅

### 异常输入核验

- 重复时间戳 → exit 1，`duplicate_timestamp`，定位到样本 2；
  设 `drop_duplicates=true` 后可正常积分。
- 缺样（0.29 s 间隔，max_dt=0.05 s）→ exit 1，`missing_samples`，定位到样本 2。
- 角速度单位错误（500 当 rad/s）→ exit 1，`gyro_unit_error`；
  同一请求改声明 `gyro_unit="deg/s"` 后校验通过（8.727 rad/s）。
- 时间倒序默认报 `non_monotonic_time`，`sort=true` 可自动排序。

### 测试命令与结果

```bash
$ python3 -m pytest -q
................................................................
62 passed in 2.01s
```

测试分布：四元数数学 13 个；积分/合成场景 16 个（静止、匀速转动、恒加速度、
变 dt、偏置补偿与未补偿漂移、deg/s 输入、倾斜初始姿态、初速度等）；
校验与错误码 17 个；JSON 入口与 CLI（含子进程）16 个。

开发过程中首轮测试曾出现 3 个失败项，均已定位并修复（如实记录）：

1. **真实缺陷**：`cli validate` 分支错误访问只有 `run` 子命令才有的
   `args.output`，导致 validate 报错路径直接抛 `AttributeError`。已修复为仅
   `run` 且指定 `-o` 时写文件。回归测试 `test_cli_validate_detects_duplicate` 覆盖。
2. **真实缺陷**：内置 acceleration demo 的 `duration=2.0` 与 dt 数组之和 1.0
   不一致，被自身校验拒绝。已改为 1.0。
3. **测试期望错误**：去重测试中区间数期望写成 4，实际插入重复样本后共 6 条、
   去重后仍 6 条 = 5 个区间。已修正断言。

修复后全量 62 个用例通过，无未通过项、无跳过项。

## 已知限制

- 一阶/中点数值积分：绕水平轴快速转动时平移存在与 dt 成正比的离散残差
  （测试以 dt=1e-3 验证有界），不做高阶圆锥/划摇补偿。
- 偏置固定且已知，不在线估计；无噪声模型、无置信度输出。
- JSON 入口为库函数 + 文件/stdio CLI，未内置 HTTP 服务（可直接把
  `process_request` 包进任意 Web 框架）。
