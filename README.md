# 曲线简化误差界（Curve Polyline Simplification — Error Bound）

纯后端、无地图、无前端的离线空间计算程序。对一条二维折线做
Douglas–Peucker（DP）简化，并输出**每个原始点到简化折线距离**的
逐点误差验证报告。输出仅包含坐标与数值（JSON）。

- 语言/标准：C++17（仅标准库，无第三方依赖）
- 入口：可执行文件 `build/simplify`，从 **stdin** 或**文件参数**读取一个 JSON 请求，
  向 stdout 写一个 JSON 响应
- 不做任何坐标投影、不渲染地图、不含网络服务

---

## 1. 坐标系、单位与精度约定（务必先读）

| 项目 | 约定 |
|---|---|
| 坐标系 | **平面笛卡尔二维坐标**，字段 `coordinate_system = "planar_cartesian_2d_unitless_double"`。不假设经纬度，不做投影；如输入是经纬度，距离是“角度单位”下的平面欧氏距离，并非米。 |
| 单位 | 坐标与 `epsilon`、输出距离**同单位**，程序不解释单位含义。 |
| 数值类型 | IEEE 754 binary64（C++ `double`，约 15–17 位十进制有效数字）。 |
| 数值序列化 | `%.17g`，保证每个 binary64 值可无损往返（round-trip）。因此 `0.95` 在输出中显示为 `0.94999999999999996`，这是同一 binary64 值的精确十进制表示，不是误差。 |
| 距离度量 | 欧氏距离。点到折线 = 到各组成线段距离的**最小值**。 |
| 点到线段 | 点到**闭线段**的距离：参数 `t = clamp(((P−A)·(B−A))/|B−A|², 0, 1)`，最近点 `A + t(B−A)`。垂足落在线段之外时取**端点**，距离用 `hypot` 计算。 |
| 非有限数 | `NaN`、`+/-Infinity` 的坐标或 epsilon 一律拒绝（解析阶段或语义阶段报错）。 |

> 关键选择：DP 的分裂判据使用“点到**线段**”距离，而**不是**某些教材写法的
> “点到弦所在**无限延长线**的垂直距离”。这一选择使本程序声明的误差界
> （第 3 节）对任意折线——包括回折、自交——都成立。

---

## 2. 算法：Douglas–Peucker

1. **预处理（去重）**：删除与上一个保留点完全相同（`x`、`y` 位级相等）的相邻点，
   消除零长度线段。只压缩**相邻**重复；非相邻重复（回折造成）保留。
   被删点的原始下标在 `removed_duplicate_point_indices` 中给出。
2. 保留折线两个端点。
3. 对当前区间 `[lo, hi]`，求开区间内距弦线段 `[P_lo, P_hi]` 最远的点：
   - 若其距离 **严格大于** `epsilon`（`d > epsilon`），标记该点保留，
     并把 `[lo, idx]`、`[idx, hi]` 两个子区间压栈；
   - 否则该区间不再分裂。
4. 用显式栈迭代实现（`src/simplification.cpp`），避免长折线深递归导致栈溢出。

**确定性保证**（相同输入与 epsilon 必然逐字节相同输出）：

- 并列最远点时取**下标最小**者（从左向右扫描，仅在严格更大时更新）；
- 逐点验证里“最近线段”并列时也取最小线段下标；
- 不使用任何随机数、多线程或与迭代顺序相关的浮点归约
  （距离取最小值，而非求和）。
- 端到端测试对同一请求连跑 5 次断言输出逐字节一致。

边界阈值：

- `d == epsilon` 的点**不保留**（严格 `>` 判据）。
- `epsilon = 0`：只有计算距离恰为 0（落在弦线段上）的点被丢弃，即去除共线/重合点；
  任何真实偏离都会被保留。

---

## 3. 误差界声明（单侧，**不是**双向 Hausdorff）

本程序**只**声明并验证下面的**单侧**界：

> 对每一个原始输入点 `P_i`：
> `dist(P_i, 简化折线 S) ≤ epsilon`，
> 其中 `dist` 是点到折线上所有线段（含端点）欧氏距离的最小值。

响应中的 `verification.bound_semantics` 固定为
`"one_sided_original_to_simplified"`，逐点结果在 `verification.points[]`，
汇总最大值在 `verification.max_distance`，整体结论在 `verification.within_bound`。

**为什么这个界成立（对回折、自交也成立）**：DP 结束时，每个被保留的相邻点对
`(Q_k, Q_{k+1})` 对应原始折线的一段 `P_a..P_b`，该段上没有任何点在该段的
递归处理中触发分裂，故段内每个点到弦线段 `[Q_k, Q_{k+1}]` 的距离都 `≤ epsilon`。
而这条弦本身就是简化折线的一条组成线段，所以点到**整条**简化折线的距离
（取所有线段距离的最小值）也 `≤ epsilon`。该论证是逐段的，不依赖折线
“不回折/不自交”，因此对回折、自交输入同样有效。

**本程序明确不声明的内容**：

- **不声明双向 Hausdorff 界。** 双向 Hausdorff 距离还要考虑
  “简化折线上的点到原始折线”这一方向。DP 只保证“原始 → 简化”方向，
  反向一般**不受** `epsilon` 控制。例如回折被剪掉后，简化弦上可能存在
  远离原始轨迹的点。
- 不做拓扑保持、不保证不自交性变化、不保持折线方向/面积。
- 不声称对“无限延长线垂直距离”变体的界；本程序只对上述点到闭线段度量负责。

**数值容差**：浮点比较允许一个与尺度成比例的舍入界

```
numerical_tolerance = 16 * 2^-52 * max(epsilon, max|坐标分量|, 1)
```

判定逐点是否满足时用 `d <= epsilon + numerical_tolerance`。这是 binary64
乘加/`hypot` 的舍入余量，不是算法裕量；`numerical_tolerance` 会写进响应。

---

## 4. 退化情形处理

| 情形 | 行为 |
|---|---|
| 空点数组 `[]` | 合法。简化结果为空，`max_distance = 0`，界视为成立（没有点需要检验）。 |
| 单点 | 合法。简化结果就是该点；逐点距离按点到点计算，`nearest_segment_index = null`。 |
| 两点 | 恒保留两点（中间无点可分裂）。 |
| 相邻重复点（零长段） | 预处理删除，下标记入 `removed_duplicate_point_indices`；这些点在逐点验证中距离为 0。 |
| 全部为同一点 | 去重后剩 1 个点，按单点处理。 |
| 非相邻重复点（回折） | **保留**并正常参与 DP；回折不做特殊处理。 |
| 自交折线 | 不检测、不特殊处理；单侧界按第 3 节成立。 |
| `epsilon = 0` | 见第 2 节，去共线/重合，保留所有真实偏离点。 |
| `epsilon < 0` | 报错 `invalid_value`，退出码 1。 |
| `NaN/Infinity` | 坐标为非有限值报错；字面上的 `NaN`/`Infinity` 因不是合法 JSON 在解析阶段拒绝，退出码 2。 |
| 缺字段 / 类型错 | 报错（`missing_field` / `invalid_type` / `invalid_value`），退出码 1。 |
| 非法 JSON / 读文件失败 | 报错（`invalid_json` / `io_error`），退出码 2。 |

---

## 5. 请求与响应格式

### 请求（stdin 或文件参数）

```json
{
  "epsilon": 0.95,
  "points": [ {"x": 0, "y": 0}, {"x": 2, "y": 0} ]
}
```

- `epsilon`：**必填**，有限数值，`>= 0`。
- `points`：**必填**，数组；每个元素是含数值字段 `x`、`y` 的对象。
- 其它未知字段会被忽略（样例中用 `_comment` 写说明）。

### 成功响应（摘要，字段按字母序输出）

```json
{
  "status": "ok",
  "coordinate_system": "planar_cartesian_2d_unitless_double",
  "epsilon": 0.94999999999999996,
  "input_point_count": 5,
  "deduplicated_point_count": 5,
  "removed_duplicate_point_indices": [],
  "simplified": [
    {"x": 0, "y": 0,  "source_index": 0},
    {"x": 2, "y": -1, "source_index": 2},
    {"x": 4, "y": 0,  "source_index": 4}
  ],
  "simplified_count": 3,
  "verification": {
    "metric": "point_to_polyline_euclidean_min_point_segment",
    "bound_semantics": "one_sided_original_to_simplified",
    "bound": 0.94999999999999996,
    "numerical_tolerance": 1.4210854715202004e-14,
    "max_distance": 0.89442719099991586,
    "within_bound": true,
    "points": [
      {"index": 0, "x": 0, "y": 0, "distance": 0.0,
       "nearest_segment_index": 0, "within_bound": true}
    ]
  }
}
```

- `simplified[].source_index`：该保留点在**去重后**点列中的下标。
- `verification.points[]`：对**每个原始输入点**（含被去重点）逐点给出
  独立计算的距离、最近线段下标（单点简化时为 `null`）、是否满足界。
- 退出码：`0` 成功；`1` 请求语义错误；`2` IO / JSON 解析错误。

错误响应形如：

```json
{"status":"error","error":{"code":"invalid_value","message":"'epsilon' must be >= 0"}}
```

---

## 6. 构建与运行

```bash
make            # 生成 build/simplify
# stdin 方式
echo '{"epsilon":0.5,"points":[{"x":0,"y":0},{"x":1,"y":1},{"x":2,"y":0}]}' \
  | ./build/simplify
# 文件方式
./build/simplify examples/foldback.json
```

要求 g++（C++17）与 GNU make；端到端测试另需 python3（仅标准库）。

---

## 7. 自动化测试

```bash
make test       # = C++ 单元测试 + Python 端到端测试
make unit-test  # 仅 C++ 单元测试
```

- `tests/test_unit.cpp`：点到线段/折线距离、去重、DP 基本性质、
  回折、自交、零长段、`epsilon=0`、并列取最小下标、确定性、
  DP 单侧界不变量（多组形态 × 多档 epsilon）、JSON 解析与 17 位往返。
- `tests/run_e2e.py`：把可执行文件当黑盒，用 **Python 独立重算**几何量
  （不调用被测程序的距离函数），逐点核对距离数值、`within_bound` 标志、
  保留点子集/顺序/端点、去重下标、误差界；并覆盖错误输入与退出码、
  文件入口、5 次连跑字节级确定性。

实际运行结果见 [TEST_REPORT.md](TEST_REPORT.md)（含命令、输出、未通过项）。

---

## 8. 目录结构

```
src/geometry.hpp/.cpp       点、点到线段/折线距离、相邻去重
src/simplification.hpp/.cpp 迭代 Douglas–Peucker
src/json.hpp/.cpp           无依赖 JSON 解析/序列化
src/main.cpp                JSON 请求入口、校验、编排、逐点验证
tests/test_unit.cpp         C++ 单元测试
tests/run_e2e.py            Python 端到端测试（独立重算几何）
examples/*.json             请求样例（含回折、自交、重复点+零阈值、错误请求）
Makefile
test_run.log                一次真实运行的原始日志
TEST_REPORT.md              测试与运行记录
```

## 9. 复杂度与规模

- **DP 简化**：期望/常见情况 O(n log n)（递归分治），最坏情况 O(n²)
  （每个点都成为分裂点时）。迭代实现，无递归栈深度风险，额外空间 O(n)。
- **逐点验证**：朴素实现为 O(n·k)，其中 k 为保留点数（每个原始点对每条
  简化线段各算一次距离）。这是“对每个点独立报告到简化折线距离”这一输出
  定义的直接成本。对成百上千点的输入是瞬时的；200,000 点、保留约 40,000
  的压力用例在参考机器上约 132 秒（见 TEST_REPORT.md）。本工具定位为
  **离线**计算，未对验证阶段做空间索引加速；如需超大输入可后续引入
  分段/空间索引，但不改变报告的数值约定。

## 10. 明确的非目标（Non-goals）

- 不做地图渲染、瓦片、投影、坐标系转换，不提供任何前端 / HTTP 服务。
- 不输出双向 Hausdorff 距离，也不对其作任何界的承诺。
- 不做拓扑保持或自交修复；不提供 TopoJSON/GeoJSON 输出（只输出本格式的坐标与数值）。
