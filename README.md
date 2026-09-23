# 三维射线-包围盒求交后端（ray_box_backend）

纯后端、离线空间计算服务：对一条三维射线与一组**轴对齐包围盒（AABB）**做
求交，输出命中盒 id、距离参数与交点坐标（仅数值，不含任何地图/可视化/前端）。
射线遍历采用 BVH（层次包围体）加速，并在每次响应中附带与**逐盒暴力检测**的
一致性核对结果。

- 语言：C++17（仅标准库），另含一个零依赖 JSON 解析器
- 入口：`ray_box_backend`，标准输入/`--file` 读 JSON，标准输出单行 JSON
- 无网络、无前端、无图形

---

## 1. 坐标系与射线定义

- **坐标系**：右手笛卡尔坐标系，三个轴单位长度一致（不预设物理单位，
  由调用方保证所有坐标、距离同单位）。
- **射线**：`P(t) = origin + t·dir,  t ≥ 0`
  - 请求里的 `direction` **无需预先归一化**，程序内部归一化为单位向量；
  - 因此返回的 `t_enter / t_exit` 是沿射线的**欧氏距离**；
  - 只支持前向 `t ≥ 0`（射线，而非整条直线）。
- 所有坐标与中间计算使用 IEEE-754 `double`。

## 2. 边界约定、精度与退化处理

- 盒定义为**闭区间** `[min, max]`：落在面上、棱上、角点上都算命中。
  - 沿棱/面擦过且共享一段：`t_enter < t_exit`，整段为接触段；
  - 仅在一点相切（如外角平分线方向击中棱）：`t_enter == t_exit`。
- **浮点容差**：相对容差 `eps`（默认 `1e-12`，可在请求中覆盖，须在
  `(0,1)` 内）。比较时使用尺度无关的绝对容差
  `eps · max(1, |参与比较的量|)`，相切附近的舍入误差被吸收。
- **零方向分量**：采用 slab（平行平面区间）算法逐轴处理。某轴
  `dir[a] == 0` 时**不做除法**，而是检查原点常量坐标是否位于该轴 slab
  闭区间（含容差）内——从根本上避免 `0/0` 产生 `NaN/Inf`。
- **退化盒**：允许 `min[a] == max[a]`，即薄片、线段、点；`min[a] > max[a]`
  的非法盒在输入层拒绝。
- **NaN 防护**：输入中的 `NaN/Infinity` 一律拒绝；内部若出现非有限的
  `t`，该次命中被丢弃，不允许 `NaN` 进入结果或参与排序/“最近”比较
  （`NaN` 与任何数比较都为 false，会污染排序）。
- **起点在盒内**：`inside=true`、`t_enter=0`、`point_enter=origin`，
  `t_exit` 为沿方向到出射面的距离（起点在边界容差带内同此处理）。
- **t=0 的边界接触**：起点恰在盒面上但方向朝外时，闭区间约定下仍算命中，
  即 `t_enter=t_exit=0`、`inside=true`（命中点就是起点）；不希望计入此情形
  的调用方可自行过滤 `t_exit==0` 或把起点向外微小平移。
- 排序与并列：结果按 `(t_enter, id)` 升序；`t_enter` 相等时取较小 `id`，
  保证输出确定、可复现。

## 3. 构建

```bash
make            # 生成 ./ray_box_backend
make test       # 编译并运行 C++ 单元测试 + Python 端到端测试
make clean
```

要求：g++（支持 C++17，实测 13.3）、GNU make、Python 3（仅端到端测试需要）。

## 4. 用法

```bash
./ray_box_backend < samples/01_nearest_basic.json     # 标准输入
./ray_box_backend --file samples/01_nearest_basic.json # 文件
echo '{"mode":"nearest","ray":{...},"boxes":[...]}' | ./ray_box_backend
```

退出码：

- `0`：请求处理完成（命中、未命中、一致性结果都算成功，`ok:true`）；
- `2`：请求错误（JSON 非法、字段缺失/类型错、零方向、非法盒、重复 id 等，
  响应 `ok:false` 并带 `error` 说明）。

### 4.1 请求格式

| 字段 | 类型 | 说明 |
|---|---|---|
| `mode` | string | **必填**。`"nearest"`（最近命中）或 `"all"`（全部命中） |
| `ray.origin` | `[x,y,z]` | **必填**，三个有限数 |
| `ray.direction` | `[dx,dy,dz]` | **必填**，非零有限向量（自动归一化） |
| `boxes` | array | **必填**（可为空数组）。元素见下 |
| `eps` | number | 可选，`(0,1)` 内正数，默认 `1e-12` |

`boxes` 元素：

| 字段 | 类型 | 说明 |
|---|---|---|
| `min` / `max` | `[x,y,z]` | **必填**。逐分量 `min ≤ max` |
| `id` | int64 | 可选。缺省时取数组下标；显式给出时必须互不重复 |

未知顶层字段会被忽略（便于向前兼容）；样例文件中的 `_comment` 即利用此点。

### 4.2 响应格式

公共字段：

```jsonc
{
  "ok": true,
  "mode": "all",
  "ray_origin": [-2.0, 0.5, 0.5],
  "ray_direction_unit": [1.0, 0.0, 0.0],
  "box_count": 3,
  "bvh_node_count": 1,
  "eps": 1e-12,
  "stats": { "nodes_visited": 1, "boxes_tested": 3 },
  "consistency_check": {
    "method": "bvh_vs_bruteforce",
    "consistent": true,
    "detail": "match"
  }
  // ... 模式相关字段
}
```

`nearest` 模式：`"hit": bool`，命中时附带
`"nearest": { id, t_enter, t_exit, inside, point_enter, point_exit }`。

`all` 模式：`"hits": [ …同上结构… ]` 与 `"hit_count"`，按
`(t_enter, id)` 升序。

字段含义：

- `t_enter / t_exit`：沿单位化射线的入/出射欧氏距离；起点在盒内时
  `t_enter = 0`；单点相切时二者相等。
- `point_enter / point_exit`：`origin + t · dir_unit` 的三维坐标。
- `inside`：起点是否位于盒内（含边界容差带）。
- `stats.nodes_visited`：BVH 做过 slab 测试的节点数；
  `stats.boxes_tested`：叶子内实际做射线-盒求交的次数（可用于观察剪枝效果）。
- `consistency_check`：同一请求下 **BVH 结果与逐盒暴力遍历结果**的逐字段
  对照（id、t、交点坐标，容差同 `eps`）。生产中若出现 `consistent:false`，
  说明加速结构结果异常，应视为后端缺陷。

## 5. BVH 实现要点

- 构建：最长中心跨度轴 + 中位数划分（`nth_element`，O(n log n)），
  叶子最多 4 个盒；退化（重合中心）时退化为固定轴但仍保证划分非空。
- 遍历：显式栈（无递归深度风险），节点包围盒使用与叶子相同的 slab
  求交（含零分量与容差处理）。
- `nearest`：近侧子树优先弹出以尽早收紧最近距离；只有当节点入射参数
  **严格大于**当前最优才剪枝，相等时继续深入以保证 `id` 并列规则正确。
- `all`：不按距离剪枝，只剪掉完全不相交的子树，收集后统一排序。

## 6. 目录结构

```
include/            头文件
  geo/types.h       Vec3 / AABB
  geo/ray_box.h     射线-AABB 求交（slab 算法）
  geo/bvh.h         BVH 构建/遍历 + 逐盒参照实现
  json/json.h       最小 JSON 解析/序列化
  app/query.h       请求处理
src/                对应实现；main.cpp 为可执行入口
samples/            请求样例（含命中/盒内/擦边/退化/非法）
tests/
  test_main.cpp     C++ 单元 + 6000 条随机射线性质测试
  e2e_test.py       端到端测试（驱动二进制，校验 JSON 与退出码）
Makefile
```

## 7. 已覆盖的验收情形

起点在盒内（含负方向出射）、沿棱/角擦边（接触段与单点相切）、
负方向与斜负方向、零方向分量平行穿入/平行错过、退化盒（薄片/线段/点）、
未归一化方向、空盒集、重复 id 与 `min>max`/零方向/非有限值等非法输入，
以及随机大场景下 BVH 与逐盒检测的结果一致性与剪枝有效性。
实测记录见 `TEST_REPORT.md`。
