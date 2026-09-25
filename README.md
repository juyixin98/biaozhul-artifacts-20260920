# 曲线简化误差界（Douglas–Peucker）纯后端

折线 Douglas–Peucker 简化的**离线空间计算后端**：输入一条二维折线和容差，
输出简化折线，以及“每个原始点到简化折线距离”的逐点验证报告。

- 纯 C++17，无第三方依赖（JSON 解析/序列化自带，见 `src/json.hpp`）。
- **只输出坐标与数值**，无地图、无渲染、无前端、无网络。
- 入口为 CLI（stdin 或文件），接收一个 JSON 请求，输出一个 JSON 响应。

## 坐标系与精度约定

- 坐标一律视为**二维平面笛卡尔坐标**，单位即输入坐标单位。请求中的 `crs`
  字段（如 `"EPSG:3857"`）仅作为元数据原样回显，**不做任何投影转换、大地线
  修正或基准变换**。经纬度（EPSG:4326）度数会被直接当作平面坐标计算欧氏
  距离，其结果不是地面距离——调用方应先自行投影到平面坐标系。
- 数值类型为 IEEE 754 `double`；JSON 数字以 `%.17g` 序列化，保证 double
  往返一致。
- 判定使用直接比较；验证报告对浮点误差保留极小容差
  `slack = 1e-9 * max(1, tolerance)`，但报告同时给出原始数值（`max_distance`），
  不掩盖任何超出量。

## 误差界：只声称单方向

**声称的界（有保证、且逐点验证）**：简化后，每个原始顶点到简化折线的距离
（点到线段的最短欧氏距离）不超过 `tolerance`，即定向 Hausdorff 距离
`H(original → simplified) ≤ tolerance`（精确算术下成立）。

**明确不声称**：反方向 `H(simplified → original)` **不**受容差约束，因此
**双向（对称）Hausdorff 距离也不**受容差约束。简化弦上的点可能离所有原始
顶点都很远。示例见 `samples/request_no_bidirectional_bound.json`：

- 输入 `(0,0),(1,2),(10,0)`，容差 `2.5`；
- 简化结果为弦 `(0,0)–(10,0)`，正向最大距离为 `2 ≤ 2.5`；
- 但弦中点 `(5,0)` 到最近原始点的距离为 `√20 ≈ 4.472 > 2.5`。

响应字段 `validation.convention` 中也写明了这一范围。

## 退化情形处理

- **零长段 / 连续重复点**：点到线段的投影分母为 0 时退化为点到点距离
  （`geometry.hpp`），不产生 NaN。
- **首尾重合的闭合环**：外层弦退化为点，DP 以“到该点距离”选分裂点，环的
  拐角仍会保留。
- **全部点相同**：只保留两个端点，距离全部为 0。
- **容差为 0**：到待收缩弦距离严格大于 0 的点全部保留；**恰好**共线
  （距离精确等于 0）的中间点被删除。此行为确定且有意为之；想保留近共线点
  请传一个极小正数容差。
- **单点 / 两点输入**：原样返回。
- **空点列、负容差、非有限坐标、JSON 语法错误**：返回
  `{"status":"error", ...}`，进程退出码 1（用法错误为 2）。

## 确定性

- 最远点扫描使用严格 `>`，并列时**索引最小**者获胜；递归用显式栈、固定顺序。
- 字段顺序、数值格式固定；相同输入两次运行的输出字节完全相同（测试覆盖）。

## 构建与运行

需要 g++（实测 13.3，C++17）。

```bash
make                 # 生成 build/simplify 与 build/run_tests
./build/simplify samples/request_basic.json          # 文件参数
./build/simplify < samples/request_basic.json        # 或 stdin
./build/simplify --pretty samples/request_basic.json # 缩进输出
```

退出码：`0` 成功；`1` 输入/运行错误（错误信息仍为 JSON，写在 stdout）；
`2` 用法错误（诊断写 stderr）。

### 请求格式

```json
{
  "crs": "EPSG:3857",
  "tolerance": 1.5,
  "points": [[0.0, 0.0], [1.0, 2.0], [2.0, 0.5]]
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `crs` | 否 | 字符串，元数据，默认 `"cartesian"`，原样回显 |
| `tolerance` | 是 | 数，有限且 ≥ 0，单位同坐标 |
| `points` | 是 | `[x, y]` 数对数组，至少 1 对，坐标须有限 |

### 响应格式（成功）

`status`、`crs`、`tolerance`、`input_count`、`output_count`、
`kept_indices`（保留点在原数组中的下标，端点恒在）、`simplified`（保留点坐标），
以及：

- `validation.per_point_distance`：每个原始点到简化折线的最短距离数组；
- `validation.max_distance` / `max_distance_point_index`：上者的最大值及其下标
  （并列取下标最小者）；
- `validation.bound`：即请求容差；`bound_satisfied`：`max_distance` 是否在
  浮点 slack 内不超过容差。

## 测试

```bash
make test            # C++ 单元测试（tests/test_main.cpp）
./tests/e2e.sh       # CLI 端到端检查（tests/e2e.sh，内部会自动 make）
```

单元测试覆盖（43 项断言）：

- **回折（回折折线、原路折返）**：折叠尖点必须保留，逐点界成立；
- **自交（八字形折线）**：两种容差下逐点界成立；
- **重复点**：连续重复、整段相同点，零长弦下不出 NaN；
- **阈值为零**：非共线点全保留、精确共线中间点删除（文档化行为）；
- 闭合环（首尾重合）、单点/两点/空输入；
- 端点保留、`tolerance=2` 报告数值精确性（`max_distance = 1`）；
- 确定性：重复运行索引一致、完整管线 JSON 字节一致；
- 请求校验：负容差、空点列、畸形点、截断 JSON 等均被拒绝；
- JSON 解析/序列化往返。

端到端检查覆盖：所有样例的 `bound_satisfied`、零容差点数、回折尖点下标、
输出无 NaN、非法请求退出码 1、stdin 输入、两次输出字节一致、`--pretty`。

### 实跑记录（本机）

环境：Linux 6.8，g++ 13.3.0，无 cmake（故使用 Makefile）。

```text
$ make
g++ -std=c++17 -O2 -Wall -Wextra -pedantic -o build/simplify src/main.cpp
g++ -std=c++17 -O2 -Wall -Wextra -pedantic -o build/run_tests tests/test_main.cpp
（无编译警告）

$ ./build/run_tests
43 checks, 0 failures

$ ./tests/e2e.sh
13 项检查全部 ok，输出 ALL E2E CHECKS PASSED
```

`request_basic.json`（容差 1.5，7→5 点）报告片段：

```json
"kept_indices": [0, 1, 3, 5, 6],
"per_point_distance": [0, 0, 0.22360679774997896, 0, 0, 0, 0],
"max_distance": 0.22360679774997896,
"max_distance_point_index": 2,
"bound": 1.5,
"bound_satisfied": true
```

`request_invalid.json`（容差 -1）：stdout 为
`{"status":"error","error":"\"tolerance\" must be >= 0"}`，退出码 1。

**未通过项：无。** 所有单元测试与端到端检查均通过；不存在已知未修复失败。

## 目录结构

```
src/geometry.hpp          点-线段/点-折线距离（含零长段退化）
src/douglas_peucker.hpp   DP 简化（显式栈、保端点、确定性规则）
src/json.hpp              最小 JSON 解析/序列化
src/service.hpp           请求校验 + 简化 + 验证报告
src/main.cpp              CLI 入口
tests/test_main.cpp       单元测试
tests/e2e.sh              端到端检查
samples/*.json            请求样例（含零容差、回折、重复点/自交、反例、非法请求）
Makefile
```
