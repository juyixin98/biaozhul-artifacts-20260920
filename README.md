# 轨迹时间插值后端 (trajectory-interp)

纯后端离线空间计算服务：对带时间戳的三维位姿序列做时间插值。位置线性插值，
旋转四元数 SLERP。JSON 请求入口，仅输出坐标与数值；不含地图、不含前端。

## 约定

| 项 | 约定 |
|---|---|
| 坐标系 | 右手系，位置单位米 |
| 旋转表示 | 单位四元数 `[w, x, y, z]`，表示机体坐标系到参考（世界）坐标系的旋转 |
| 时间 | 秒，IEEE 754 双精度 |
| 精度 | 全部计算双精度；输出 `%.17g`（可无损回读 double）；四元数输出范数与 1 的偏差 ≤ 1e-12（SLERP 构造保证，测试按 ≤ 1e-9 抽检） |
| 符号等价 | `q` 与 `-q` 表示同一旋转；SLERP 在点积为负时翻转第二操作数符号，永远走最短路径 |

## 插值与退化处理

- **位置**：`p(u) = (1-u)·p0 + u·p1`，`u = (t - t0) / (t1 - t0)`。
- **旋转**：标准 SLERP。`dot = q0·q1`；`dot < 0` 时翻转 `q1` 符号（符号等价、最短路径）。
- **小角度退化**：`|dot| > 1 - 1e-12`（约 2 微弧度以内）时退化为归一化线性插值
  （NLERP），避免 `sin(θ) → 0` 的 0/0 不稳定；端点完全相同时结果恒等于该旋转。
- **180° 附近**：旋转差接近 180° 时四元数点积接近 0，`sin(θ) ≈ 1`，SLERP 公式
  条件良好，无需特殊处理；测试覆盖 179.9° 与恰好 180°。
- **恰好对跖**（`q1 == -q0`，四元数空间 180°）：符号翻转后端点重合，结果恒为
  该旋转，不产生 NaN。
- **输入四元数**：加载时归一化；零范数四元数拒绝（`invalid_trajectory`）。

## 时间轴策略（明确）

- **重复时间戳**：加载时拒绝，报错 `invalid_trajectory: duplicate timestamp`，
  不做静默合并。
- **外推**：`options.extrapolation` —
  - `"clamp"`（默认）：超出关键帧时间范围时返回首/末位姿（位姿时间戳为查询时刻）；
  - `"error"`：超范围查询返回 `out_of_range` 错误。
- **断档拒绝**：设置 `options.max_gap_seconds` 后，相邻关键帧间隔**严格大于**该值的
  区间视为数据断档；落在断档区间内部（不含端点）的查询返回 `gap` 错误。
  恰好等于 `max_gap_seconds` 的区间不算断档。关键帧时刻本身的查询永远成功。
- **单关键帧**：轨迹退化为常量，范围内查询返回该位姿，范围外按外推策略处理。
- **输入顺序**：关键帧可乱序，加载时按时间排序。

## 构建与测试

依赖：C++17 编译器（g++ ≥ 9）与 make，无第三方库。

```sh
make            # 构建 build/trajectory_interp 与 build/test_trajectory
make test       # 运行自动化测试（42 项检查）
```

## 使用

```sh
./build/trajectory_interp request.json     # 从文件读请求
./build/trajectory_interp < request.json   # 从标准输入读
```

### 请求

```json
{
  "poses": [
    {"t": 0.0, "position": [0.0, 0.0, 0.0], "orientation": [1.0, 0.0, 0.0, 0.0]},
    {"t": 1.0, "position": [2.0, 0.0, 1.0], "orientation": [0.7071067811865476, 0.0, 0.7071067811865476, 0.0]}
  ],
  "queries": [0.0, 0.5, 1.5],
  "options": {"extrapolation": "clamp", "max_gap_seconds": 5.0}
}
```

- `poses[].orientation`：`[w, x, y, z]`，允许非单位四元数（加载时归一化）。
- `options` 可省略；`extrapolation` 默认 `"clamp"`，不设置 `max_gap_seconds` 则不检查断档。

### 响应

每个查询一条结果，逐条独立成败（退出码仍为 0）：

```json
{"results":[
  {"t":0.5,"status":"ok","position":[1,0,0.5],"orientation":[0.92387953251128685,0,0.38268343236508984,0]},
  {"t":5,"status":"error","error":{"code":"gap","message":"query falls inside a data gap longer than max_gap_seconds"}}
]}
```

请求级失败（JSON 语法错误、字段缺失、重复时间戳、空轨迹、零范数四元数等）
返回 `{"error":{"code":...,"message":...}}`，退出码 2。错误码：
`invalid_json`、`invalid_request`、`invalid_trajectory`（请求级）；
`out_of_range`、`gap`、`invalid_query`（查询级）。

样例请求见 `examples/`：`basic.json`（正常插值）、`gap.json`（断档拒绝）、
`extrapolation_error.json`（外推报错）、`duplicate_timestamps.json`（重复时间戳拒绝）。

## 测试覆盖（验收项）

`tests/test_main.cpp`，共 42 项检查：

- **180° 附近**：179.9° 与恰好 180° 旋转，中点为半角旋转，全程单位范数、转轴不变；
- **小角度**：1 微弧度旋转走 NLERP 退化路径，中点为半角；相同端点输出常量；
- **相反符号四元数**：`q` 与 `-q` 轨迹旋转处处一致，内部采样逐位相同，走最短路径；
- **恰好对跖**：`q1 == -q0` 输出常量旋转、无 NaN；
- **单位范数**：多段 1001 点扫描全部满足 `|q| = 1`（容差 1e-9）；
- **端点一致**：关键帧时刻查询逐位返回输入位姿（位置与归一化后四元数）；
- **断档拒绝**：断档内部查询返回 `gap`，断档端点与相邻正常区间不受影响，
  等于 `max_gap_seconds` 的区间放行；
- **策略**：重复时间戳拒绝、外推 clamp/error 两种模式、乱序输入排序、
  单关键帧常量、零范数/NaN/空轨迹拒绝。

## 实际运行记录

环境：Ubuntu 24.04，g++ 13.3.0，GNU Make 4.3（无 cmake，故使用 Makefile）。

```
$ make && make test
g++ -std=c++17 -O2 -Wall -Wextra -pedantic src/main.cpp src/trajectory.cpp -o build/trajectory_interp
g++ -std=c++17 -O2 -Wall -Wextra -pedantic tests/test_main.cpp src/trajectory.cpp -o build/test_trajectory
./build/test_trajectory
42 checks, 0 failures        # 全部通过，无未通过项
```

（首轮曾因测试用例在关键帧时刻用逐位比较 `-q` 端点而失败 1 项——属测试断言问题，
端点按约定原样返回存储值；改用旋转等价比较后通过。）

CLI 样例（完整输出见上文格式）：

```
$ ./build/trajectory_interp examples/basic.json
{"results":[...4 条全部 "status":"ok"...]}                                   exit=0
$ ./build/trajectory_interp examples/gap.json
...t=5 → "status":"error","error":{"code":"gap"...}，其余 ok...               exit=0
$ ./build/trajectory_interp examples/extrapolation_error.json
...t=-0.5 与 t=1.5 → "out_of_range"，t=0.5 ok...                              exit=0
$ ./build/trajectory_interp examples/duplicate_timestamps.json
{"error":{"code":"invalid_trajectory","message":"duplicate timestamp 0.000000"}}  exit=2
```

## 目录结构

```
src/json.hpp         最小 JSON 解析/序列化（无第三方依赖）
src/geometry.hpp     Vec3 / Quat / SLERP（含符号等价与退化处理）
src/trajectory.hpp   轨迹类：策略定义与查询接口
src/trajectory.cpp
src/main.cpp         JSON 请求入口（CLI）
tests/test_main.cpp  自动化测试（42 项检查）
examples/            请求样例
Makefile
```
