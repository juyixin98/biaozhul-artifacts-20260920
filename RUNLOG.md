# 运行记录（RUNLOG）

环境：Linux 6.8.0-90-generic，Python 3.12.3，NumPy 2.5.3，pytest 9.1.1。
以下命令与输出均为实际执行结果，按时间顺序如实记录，包括初次失败项。

## 1. 自动化测试

### 第一次运行（实现完成后）

命令：

```bash
python3 -m pytest tests/ -q
```

结果：**40 passed, 3 failed**

失败项：

1. `tests/test_kinematics.py::test_plan_segment_too_short_to_decelerate`
2. `tests/test_kinematics.py::test_plan_segment_too_short_to_accelerate`

   原因：`kinematics.plan_segment` 中加速能力/制动能力两条
   `InfeasibleTrajectory` 的错误消息写反（判定本身正确，文案与
   `match=` 断言不符）。修复：交换两条消息对应的分支。

3. `tests/test_planner.py::test_plan_from_request_serializable`

   原因：`profile.validate_profile` 返回的 checks 字典中布尔值为
   `numpy.bool_`，`json.dumps` 无法序列化
   （`TypeError: Object of type bool is not JSON serializable`）。
   修复：对每个 check 显式 `bool(...)` 转换。

### 第二次运行（修复后）

命令：

```bash
python3 -m pytest tests/ -q
```

结果：**43 passed in 3.41s**

### 最终详细运行

命令：

```bash
python3 -m pytest tests/ -v
```

结果：**43 passed in 2.96s**，覆盖：

- 几何：长度、转角（直角/折返尖点）、重复点折叠、零长段不产生 NaN、
  非法输入报错（6 项）
- 运动学：前向/后向传播、零长段同速、三角形/梯形闭式解、非对称端点、
  零长段、加/减速距离不足不可行、非正加速度参数（13 项）
- 端到端规划：直线解析时长与限值、端点位置、尖角停点速度为 0、
  曲率限速 `sqrt(a_lat·R)`、缺 a_lat/曲率报错、尖点强制停车、
  显式停点、非零起终速度、不可行初速检测、零长段端到端、
  时间单调与位置连续、JSON 可序列化（16 项）
- JSON 入口与合成传感器：全部示例、CLI 文件/stdin 输入、
  退出码 0/1/2、合成里程计误差量级（8 项）

## 2. 示例运行

### 直线（解析对照：3 m，v_max=1，a=d=0.5 -> 2s 加速 + 1s 巡航 + 2s 制动 = 5s）

命令：

```bash
python3 -m trajectory_planning.json_entry examples/request_straight.json --pretty
```

关键结果：

```
status: ok
total_time: 5.0        （与手算一致）
start_speed / end_speed: 0.0 / 0.0
segment[0]: v_peak=1.0, t_acc=2.0, t_coast=1.0, t_dec=2.0
validation.passed: true
max_speed=1.0, max_accel_numeric=0.5, max_decel_numeric=0.5
violations: speed=0, accel=0, decel=0
```

### 尖角停点（2m + 2m 直角，stop 策略，含合成里程计）

命令：

```bash
python3 -m trajectory_planning.json_entry examples/request_sharp_corner_stop.json
```

结果：

```
status ok | total_time=8.0000 | node_speeds=[0.0, 0.0, 0.0]
validation.passed=True | max_speed=1.0 max_acc=0.5 max_dec=0.5
合成里程计误差（seed=7，401 个采样）:
  position RMSE=0.00134 m, max=0.00329 m（噪声设定 0.001 m）
  speed    RMSE=0.00969 m/s, max=0.0257 m/s（噪声设定 0.01 m/s）
```

总时长 8s 与解析一致：每段 2m 起停（三角形，峰值
sqrt(2·0.5·2)=sqrt(2)，单段时长 2·sqrt(2/0.5)=4s），两段共 8s。

### 曲率限速（直角，R=0.5 m，a_lat=0.6）

命令：

```bash
python3 -m trajectory_planning.json_entry examples/request_corner_curvature.json
```

结果：

```
status ok | total_time=5.2451 | node_speeds=[0.0, 0.5477, 0.0]
validation.passed=True | max_speed=1.3226 max_acc=0.8 max_dec=0.8
```

拐角速度 0.5477 = sqrt(0.6·0.5) = sqrt(0.3)，与公式一致；
全程速度未超 v_max=1.5。

### 零长段（输入含相邻重复点）

命令：

```bash
python3 -m trajectory_planning.json_entry examples/request_zero_length.json
```

结果：

```
status ok | removed_duplicate_nodes=1 | total_time=8.4853
node_speeds=[0.0, 0.0, 0.0, 0.0] | validation.passed=True
max_speed=0.7071 max_acc=0.5 max_dec=0.5（全部有限，无除零/NaN）
```

### 不可行请求（3 m/s 起步，0.1 m 内刹停，d_max=0.5）

命令：

```bash
python3 -m trajectory_planning.json_entry examples/request_infeasible.json
```

结果（退出码 1）：

```
status error | InfeasibleTrajectory
起点速度 3 m/s 不可行：路径长度 0.1 m 无法在减速度上限 0.5 m/s^2
内安全到达终点速度 0 m/s（起点最多允许 0.3162 m/s）
```

系统如实拒绝而非静默压低起点速度；0.3162 = sqrt(2·0.5·0.1)。

## 3. 未通过项 / 已知限制

- 首次测试运行的 3 个失败项已全部修复并通过（见上），最终无未通过项。
- 数值校验的加/减速容差为差分误差 +1e-3 m/s²（1 ms 采样、中心差分）；
  所有用例实测违反量为 0 或在数值误差量级。
- `curvature` 策略中的半径是用户显式给定的单段圆角等效曲率；
  系统会在圆角切深 `R·tan(δ/2)` 超出相邻段长时给出 warning，
  但不会自动修改几何（不发明用户未提供的曲线）。
- 未实现连续曲率（样条）平滑、加加速度（jerk）约束与时间最优
  数值积分（TOPP 类）方法——超出本次"沿折线 + 明确拐角策略"的范围。
