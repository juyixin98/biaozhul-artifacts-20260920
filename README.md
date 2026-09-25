# 轨迹时间插值后端 (traj_interp)

纯后端离线空间计算服务:对带时间戳的三维位姿序列做时间插值,通过 JSON
请求/响应交互,**仅输出坐标与数值**,不含地图、不含前端。

## 坐标系与精度约定

| 项 | 约定 |
|---|---|
| 坐标系 | 右手系;位置 `[x, y, z]`,单位米 (m) |
| 姿态 | 单位四元数 `[w, x, y, z]`(标量在前);`q` 与 `-q` 表示同一旋转(符号等价) |
| 时间戳 | `double`,单位秒 (s),单调**严格递增** |
| 计算精度 | 全程 IEEE 754 double;输出 17 位有效数字,可无损往返 |
| 输出范数 | 所有输出四元数重新归一化,单位范数误差 ≤ 1e-12 |

## 插值模型

- **位置**:按时间参数 `u = (t - t0) / (t1 - t0)` 线性插值 (lerp)。
- **姿态**:球面线性插值 (SLERP),与位置共用同一参数 `u`。

### 退化与边界处理(显式策略)

1. **符号等价**:`dot(q0, q1) < 0` 时翻转 `q1` 取短弧,`q` 与 `-q` 插值结果恒定。
2. **小角度**:`dot > 0.9995`(约 < 1.8°)时 `sin(θ)` 接近 0,SLERP 系数数值
   不稳定,退化为归一化线性插值 (nlerp)。
3. **180° 附近**:`dot ≈ 0`、`sin(θ) ≈ 1`,标准公式天然稳定,无需特判;
   测试覆盖 179.9° / 179.999999° / 恰好 180°。
4. **重复时间戳**:输入非法,构造轨迹时拒绝(`invalid_trajectory`)。
5. **外推**:不支持。查询时间超出 `[t_first, t_last]` 一律拒绝
   (`out_of_range`),即断档拒绝;单点轨迹仅 `t == t0` 可查询。
6. **端点**:查询精确命中 `t_first` / `t_last` 时原样返回端点位姿。
7. **零范数 / 非有限四元数**:拒绝(`invalid_trajectory`);非单位但范数
   正常的四元数在构造时归一化。

## 构建与测试

依赖:g++(C++17)与 make,无第三方库。

```sh
make          # 构建 build/traj_interp 与 build/test_interp
make test     # 运行自动化测试
make clean
```

## 使用

```sh
./build/traj_interp request.json     # 从文件读请求
cat request.json | ./build/traj_interp -   # 或从 stdin
```

### 请求格式

```json
{
  "trajectory": [
    {"t": 0.0, "position": [0.0, 0.0, 0.0], "orientation": [1.0, 0.0, 0.0, 0.0]},
    {"t": 1.0, "position": [2.0, 0.0, 0.5], "orientation": [0.7071067811865476, 0.0, 0.0, 0.7071067811865476]}
  ],
  "queries": [0.0, 0.5]
}
```

### 响应格式

```json
{
  "ok": true,
  "frame": {"position_unit": "m", "time_unit": "s",
            "orientation": "unit quaternion (w,x,y,z)",
            "handedness": "right",
            "extrapolation": "rejected (out_of_range)"},
  "results": [
    {"t": 0.5, "ok": true, "position": [1, 0, 0.25],
     "orientation": [0.9238795325112868, 0, 0, 0.38268343236508984]},
    {"t": 9.9, "ok": false, "error": "out_of_range"}
  ]
}
```

- 退出码 `0`:请求已处理(单个查询失败不影响,见各 `results[i].ok`)。
- 退出码 `1`:请求级错误,响应为
  `{"ok": false, "error": "<code>", "message": "<detail>"}`,
  `error` ∈ `io_error` / `parse_error` / `bad_request` / `invalid_trajectory`。

## 请求样例

- `examples/request_basic.json` — 三段轨迹,含 90°→180° 绕 z 旋转。
- `examples/request_edge.json` — 反号四元数(符号等价)+ 范围外查询拒绝。

## 自动化测试

`tests/test_interp.cpp`(自包含,35 项断言)覆盖:

- 位置线性插值中点;SLERP 90° 中点为 45°
- **小角度**(0.001°):单位范数 + 角度单调
- **相反符号四元数**:插值结果恒定、不翻转、走短弧
- **180° 附近**:恰好 180° 中点为 90°;179.9° / 179.999999° 全程单位范数
- **端点一致性**:`t_first` / `t_last` 精确返回原始位姿
- **断档拒绝**:范围外、NaN 查询返回 `out_of_range`
- **重复时间戳拒绝**、乱序拒绝、零范数四元数拒绝
- 非单位四元数输入归一化;多段轨迹跨段单位范数;JSON 解析/序列化往返

## 实际运行记录(2026-09-24, g++ 13.3.0, Linux 6.8)

```
$ make
g++ -std=c++17 -O2 -Wall -Wextra -Wpedantic -Iinclude -o build/traj_interp src/main.cpp
g++ -std=c++17 -O2 -Wall -Wextra -Wpedantic -Iinclude -o build/test_interp tests/test_interp.cpp

$ make test
./build/test_interp
35/35 checks passed
ALL TESTS PASSED

$ ./build/traj_interp examples/request_edge.json
{"frame":{...},"ok":true,"results":[
  {"error":"out_of_range","ok":false,"t":-1},
  {"ok":true,"orientation":[0.92387953251128685,0,0,0.38268343236508984],
   "position":[0.5,0,0],"t":10.5},
  {"error":"out_of_range","ok":false,"t":11.5}]}
(exit=0;反号 90° 四元数正确走短弧,中点为 45°)

$ echo '{"trajectory":[{"t":0,...},{"t":0,...}],"queries":[0]}' | ./build/traj_interp
{"error":"invalid_trajectory","message":"timestamps not strictly increasing
 (duplicate or out-of-order) at index 1","ok":false}   (exit=1)

$ echo 'not json' | ./build/traj_interp
{"error":"parse_error","message":"JSON parse error at offset 2: ...","ok":false}   (exit=1)
```

**未通过项:无。** 35/35 断言全部通过;构建无警告(-Wall -Wextra -Wpedantic)。

## 目录结构

```
include/traj/quat.hpp        # 四元数/向量,SLERP(含退化处理)
include/traj/trajectory.hpp  # 位姿序列、校验、时间插值
include/traj/json.hpp        # 最小 JSON 解析/序列化(无外部依赖)
src/main.cpp                 # JSON 请求入口(CLI)
tests/test_interp.cpp        # 自动化测试(35 项断言)
examples/*.json              # 请求样例
Makefile
```
