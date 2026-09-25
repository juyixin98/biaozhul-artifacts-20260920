# 多边形裁剪子集（纯后端）

离线空间计算后端：给定一个**简单多边形**（subject）与一个**凸多边形**
（clip），计算它们的交集，输出**有序、方向统一（CCW）的顶点**以及面积等
数值。本项目只输出坐标与数值，**不含任何地图、渲染或前端**。

- 语言：C++17（无第三方依赖），JSON 解析/序列化为自带最小实现
- 算法：Sutherland–Hodgman 逐边裁剪（凸裁剪器，凹主体可处理）
- 入口：单一命令行二进制，JSON 请求 → JSON 响应（stdin/文件）
- 测试：C++ 单元测试 + Python 端到端测试 + 随机模糊测试（独立参考实现）

---

## 1. 构建

需要 g++（支持 C++17）与 GNU make。

```bash
make            # 生成 build/polygon-clip
```

## 2. 使用

```bash
# 从 stdin
./build/polygon-clip < examples/01_triangle_clip.json

# 从文件
./build/polygon-clip examples/01_triangle_clip.json

# 写入文件
./build/polygon-clip examples/01_triangle_clip.json -o /tmp/out.json
```

退出码：

| 码 | 含义 |
|----|------|
| 0  | 成功（含空交集 / 退化结果，响应 `status` 仍为 `ok`） |
| 1  | 用法 / IO 错误 |
| 2  | JSON 语法错误（`INVALID_JSON`） |
| 3  | 请求结构错误（`INVALID_REQUEST`） |
| 4  | 几何校验或结果错误（`INVALID_SUBJECT` / `INVALID_CLIP` / `RESULT_*`） |

---

## 3. 坐标系、方向与精度约定（重要）

### 3.1 坐标系

- 采用**右手二维笛卡尔平面**：x 轴向右，y 轴向上。输入就是普通 `[x, y]`
  数值对，**不做任何地图投影、经纬度或单位换算**。调用方需自行保证两个多
  边形处于同一平面坐标系、同一单位。
- 多边形为**闭合环**，首尾顶点不必重复（系统按环处理，`v[n-1]→v[0]`）。

### 3.2 方向（绕序）

- **CCW** = 逆时针（数学正向，有向面积为正）；**CW** = 顺时针。
- 输入主体允许 CW 或 CCW；裁剪器允许 CW 或 CCW（内部统一规范化为 CCW）。
- **输出只要有面积，方向一律为 CCW**，并在 `result.orientation` 中报告；
  主体原始方向见 `result.input_subject_orientation`。

### 3.3 精度与容差

- 输入数值按 IEEE-754 `double` 解析；**全部几何计算使用 `long double`**
  （x86-64 上为 80 位扩展精度），面积用鞋带公式（shoelace）计算。
- 输出坐标/数值转回 `double`，默认以最短可往返十进制形式输出；
  `decimal_places` 可要求固定小数位（0–15）。
- 几何判定使用**自适应线性容差** `epsilon`（默认 `1e-9`，单位与坐标相同）：
  - 叉积判定阈值 ≈ `epsilon * max(1, 边长)`；
  - 面积退化阈值 ≈ `epsilon * max(1, 包围尺度)^2`。
  - 因此 1e-9 适用于坐标量级约 1 的数据；若坐标量级很大（如经纬度放大、
    毫米级坐标），应通过请求字段 `epsilon` 传入合适的绝对容差。
- 这是**绝对容差**模型，不是相对容差；对极大/极小坐标请显式设置 `epsilon`。

### 3.4 退化结果的分类

凸集 ∩ 简单多边形在拓扑上恒为凸集，因此结果只可能是四类（`result.kind`）：

| kind | 含义 | 顶点 | 面积 |
|------|------|------|------|
| `empty` | 无交（无公共点） | `[]` | 0 |
| `point` | 退化为一个点（如仅顶点接触） | 1 个 | 0 |
| `segment` | 退化为一条线段（如仅沿边重合） | 2 个 | 0 |
| `polygon` | 有面积的凸/凹简单多边形 | ≥3 个，CCW | >0 |

连续共线的冗余折点会被移除；面积低于退化阈值时归为 point/segment。
极薄但面积非零的结果仍报 `polygon`，同时在 `result.warnings` 给出
sliver（细条）提示。

### 3.5 输入自交与边界接触策略

- **主体（subject）必须是严格简单多边形**：非相邻边不得相交或端点接触，
  也不允许回折尖刺（A–B–A 型共线折返）。违反返回 `INVALID_SUBJECT`。
  相邻共线但折点位于两邻居之间的冗余点允许（会被自动去除）。
- **裁剪器（clip）必须是严格凸多边形**（允许共线冗余点）。非凸或全共线
  返回 `INVALID_CLIP`。
- **边界接触不是错误**：点接触、沿边重合分别得到 `point` / `segment` /
  正常 `polygon`，语义明确。
- 凹主体与凸裁剪器的交集**本应**仍为凸集；但 Sutherland–Hodgman 的单环
  输出在交集**不连通**（如 U 形被横切出两段）时会产生沿裁剪边回折的自接
  触环。系统检测到这种情况会**显式报错** `RESULT_MULTIPLE_COMPONENTS` /
  `RESULT_NOT_SIMPLE`，而不是静默返回错误几何。（本任务只承诺输出单个简单
  多边形，多组件结果不在输出能力内。）

---

## 4. JSON 请求 / 响应契约

### 4.1 请求

```json
{
  "subject": [[x, y], ...],
  "clip":    [[x, y], ...],
  "epsilon": 1e-9,
  "decimal_places": 12
}
```

| 字段 | 必需 | 说明 |
|------|------|------|
| `subject` | 是 | 简单多边形顶点，≥3 个 `[x,y]`，有限数值 |
| `clip` | 是 | 凸多边形顶点，≥3 个 `[x,y]`，有限数值 |
| `epsilon` | 否 | 线性绝对容差，(0,1]，默认 1e-9 |
| `decimal_places` | 否 | 输出固定小数位，整数 0–15；缺省用最短往返表示 |

### 4.2 成功响应

```json
{
  "status": "ok",
  "metrics": {
    "area": 63.111111111111114,
    "signed_area": 63.111111111111114,
    "perimeter": 32.314161756542383,
    "epsilon": 1e-9
  },
  "result": {
    "kind": "polygon",
    "orientation": "CCW",
    "vertex_count": 5,
    "vertices": [[x, y], ...],
    "input_subject_orientation": "CCW",
    "warnings": []
  }
}
```

`area` 与 `signed_area` 对 CCW 结果相同（面积非负）；`perimeter` 对
segment 为其长度，对 point/empty 为 0。

### 4.3 错误响应

```json
{
  "status": "error",
  "error": { "code": "INVALID_SUBJECT",
             "message": "subject is self-intersecting (non-adjacent edges meet)" }
}
```

错误码：`INVALID_JSON`、`INVALID_REQUEST`、`INVALID_SUBJECT`、
`INVALID_CLIP`、`RESULT_NOT_SIMPLE`、`RESULT_MULTIPLE_COMPONENTS`。

---

## 5. 手算验收案例（矩形裁剪）

裁剪器为矩形 `[0,10]×[0,10]`（CCW），主体为三角形
`(2,-2) → (18,4) → (6,16)`。

逐裁剪边求交点（线段参数 t 解线性方程）：

- 边 `(2,-2)→(18,4)`（方向 (16,6)）：与 y=0 交于 t=1/3 → **(22/3, 0)**；
  与 x=10 交于 t=1/2 → **(10, 1)**。
- 边 `(18,4)→(6,16)` 所在直线为 x+y=22，经过的是 (11,11)；矩形角点
  (10,10) 满足 x+y=20<22，**严格位于三角形内部**，故结果保留 **(10,10)**。
- 边 `(6,16)→(2,-2)`（方向 (-4,-18)）：与 y=10 交于 t=3/8 → **(14/3, 10)**；
  与 y=0 交于 t=8/9 → **(22/9, 0)**。

输出五边形（CCW，起点不保证固定，但顺序与集合确定）：

```
(22/9,0) (22/3,0) (10,1) (10,10) (14/3,10)
```

鞋带公式（按上述 CCW 顺序，Σ xᵢyᵢ₊₁ − xᵢ₊₁yᵢ）：

```
(22/9,0) -> (22/3,0):  (22/9)·0    - (22/3)·0   = 0
(22/3,0) -> (10,1):    (22/3)·1    - 10·0       = 22/3
(10,1)   -> (10,10):   10·10       - 10·1       = 90
(10,10)  -> (14/3,10): 10·10       - (14/3)·10  = 160/3
(14/3,10)-> (22/9,0):  (14/3)·0    - (22/9)·10  = -220/9

2A = 22/3 + 90 + 160/3 - 220/9
   = 66/9 + 810/9 + 480/9 - 220/9
   = 1136/9
 A = 568/9 ≈ 63.1111
```

实际程序输出面积 `63.111111111111114`、5 个 CCW 顶点，与手算一致。
（另见 `examples/01_triangle_clip.json`。）

面积界：`0 ≤ A_result ≤ min(A_subject, A_clip)`。
本例 A_clip=100，A_subject=114，0 ≤ 63.11 ≤ 100 ✓。所有结果点均位于
裁剪矩形内（由单元/端到端测试逐点断言）。

---

## 6. 其他验收情形

| 情形 | 请求样例 | 期望结果 |
|------|----------|----------|
| 全包含 | `02_fully_contained.json` | polygon，面积 4，原顶点 CCW |
| 无交 | `03_no_intersection.json` | `empty`，顶点 `[]`，面积 0 |
| 沿边重合（零面积，仅一条边接触） | `04_edge_overlap_segment.json` | `segment`，两点 (2,0),(8,0)，面积 0 |
| 自交主体（蝴蝶结） | `05_self_intersecting_rejected.json` | 退出码 4，`INVALID_SUBJECT` |
| 旋转正方形（菱形）裁剪 | `06_rotated_diamond.json` | 全包含，面积 36 |
| CW 输入 | `07_clockwise_input.json` | 输出仍 CCW，面积 4 |
| 点接触（仅顶点相切） | 见单元测试 #6 | `point`，单点 (0,10) |
| 非凸裁剪器 | 见单元测试 #8 | `INVALID_CLIP` |
| 凹主体被切成两段（断连） | 见单元测试 #11 | `RESULT_MULTIPLE_COMPONENTS` |

运行全部样例（含命令与响应）：

```bash
./scripts/run_demo.sh
```

---

## 7. 测试

```bash
make test
```

三层测试，均为实际运行可执行文件/编译产物：

1. **C++ 单元测试**（`tests/unit_tests.cpp`）：14 组、73 项断言。覆盖手算
   案例的交点与面积、全包含、无交、沿边重合（有/无面积）、点接触、CW 统一、
   非凸/自交拒绝、退化输入、菱形裁剪、面积界与结果点包含。
2. **端到端测试**（`tests/test_e2e.py`）：73 项断言。通过真实 CLI 二进制
   校验 JSON 契约、退出码、错误码、`decimal_places`、面积界与逐点包含。
3. **模糊测试**（`tests/test_fuzz.py`）：300 组随机凸裁剪器 × 随机简单主体，
   1938 项断言。用**独立 Python 参考实现**（另写一遍 Sutherland–Hodgman）
   交叉校验面积，并逐点断言结果点在裁剪域内、方向 CCW、面积界成立。

---

## 8. 实际运行记录

在 Ubuntu 24.04 / g++ 13.3.0 / x86-64 上执行。

### 8.1 干净构建

```
$ make
g++ -std=c++17 -O2 -Wall -Wextra -Wpedantic -Iinclude \
    src/geometry.cpp src/json.cpp src/main.cpp -o build/polygon-clip
```

无警告、无错误。

### 8.2 完整测试

```
$ make test
unit tests: 73 checks, 0 failure(s)
e2e tests: 73 checks, 0 failure(s)
fuzz: 300 trials, 1938 checks, 0 failure(s)
```

全部通过，无未通过项。

### 8.3 手算案例

```
$ ./build/polygon-clip < examples/01_triangle_clip.json
status=ok  kind=polygon  orientation=CCW  vertex_count=5
vertices ≈ (4.6667,10) (2.4444,0) (7.3333,0) (10,1) (10,10)
area = 63.111111111111114 = 568/9   （与第 5 节手算一致）
```

完整逐条输出见 `./scripts/run_demo.sh` 的运行结果（开发时已实际执行）。

---

## 9. 源码结构

```
include/geometry.hpp   几何接口与数据结构（Point/ClipResult/Eps/ResultKind）
src/geometry.cpp       原语、严格简单多边形/凸性校验、SH 裁剪、退化分类、自检
include/json.hpp       最小 JSON 值模型
src/json.cpp           递归下降解析 + 最短往返序列化
src/main.cpp           JSON 请求入口、参数校验、响应构造、退出码
tests/unit_tests.cpp   C++ 单元测试
tests/test_e2e.py      端到端测试（真实二进制）
tests/test_fuzz.py     随机模糊测试（独立参考实现交叉校验）
examples/              7 个请求样例
scripts/run_demo.sh    跑全部样例
Makefile
```
