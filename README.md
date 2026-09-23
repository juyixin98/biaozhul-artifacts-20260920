# 网格拓扑校验（gridtopo_check）

纯后端三角网格（triangle mesh）拓扑校验工具。读取 JSON 请求，对三角网格做
**离线空间计算**，输出顶点坐标、面 ID、边长/面积等数值与连通分量统计的 JSON 报告。
**不制作地图、不渲染图形、无前端**。

- 语言：C++17（仅标准库，零第三方依赖）
- 入口：命令行可执行文件 + JSON 请求/响应
- 平台：Linux（g++ 13 验证，任何 C++17 编译器均可）

---

## 1. 坐标系、精度与退化约定

| 项目 | 约定 |
|---|---|
| 坐标系 | 默认**右手笛卡尔直角坐标系** `(x, y, z)`。坐标系统仅作声明与透传，不做投影/变换；请求可用 `config.coordinateSystem` 覆盖（例如标注 UTM 带），计算始终按三维欧氏距离处理。 |
| 长度单位 | 默认 `unitless`；可用 `config.lengthUnit` 声明（如 `metre`）。程序不做单位换算。 |
| 数值精度 | IEEE-754 `double`（53 位尾数）。面积/距离使用双精度计算；JSON 数值以 `%.17g` 输出（17 位有效数字，double 往返精确）。 |
| 位置容差 `eps` | 默认 `1e-9`（绝对，单位同坐标）。两点欧氏距离 `<= eps` 判为**重合点**。重合关系用并查集传递合并（若 A≈B、B≈C 则 {A,B,C} 同簇，即便 A、C 距离略大于 eps）。 |
| 面积容差 `areaEps` | 默认 `1e-12`。`|AB × AC| / 2 <= areaEps` 判为**零面积三角形**。 |
| 非有限值 | 坐标为 `NaN/±Inf` 的顶点标记 `nonfinite_coordinate`；引用它的面标记 `face_uses_nonfinite_vertex`。 |

### 几何退化与拓扑错误严格分离

- **几何退化（geometric_degeneracy）**：零面积三角形、重复顶点索引（面内同一顶点出现多次）、
  重合点、非有限坐标。这些是“输入形状本身退化”，**只报告、不参与拓扑结构**
  （退化面从半边统计、连通分量中剔除）。
- **拓扑错误（topological_error）**：非流形边、重复面、孤立点。这些是面-边-顶点
  连接关系上的错误。
- **开曲面边界边不算错误**：只被一个面引用的边是 `boundary_edge`（信息项），
  开洞曲面因此可以 `status=valid`，只是 `watertight=false`。

### 边分类（拓扑核心定义）

对每条无向边统计有向半边入射：

| 半边入射情况 | 分类 |
|---|---|
| 1 个面 | 边界边 `boundary edge` |
| 2 个面，一正一反（各遍历一次） | 流形边，取向一致，无问题 |
| 2 个面同向（都 a→b 或都 b→a） | **非流形边**（该边无法在两面上一致定向；含 1 个方向冲突） |
| 3 个及以上面 | **非流形边**（`nonmanifold_edge`，报告入射面数） |

- **重复面**：三个顶点索引集合相同即判重（与绕序无关）。附加 `detail`：
  `same_winding`（绕序全部相同）、`opposite_winding`（两面相反）、
  `mixed_winding`（3+ 面中两种绕序都有）。反向重复面的边仍满足一正一反，
  因此不会同时再报非流形边，避免重复计数；同绕序重复会令 3 条边各有 2 个同向入射。
- **孤立点**：不被任何*几何有效*面引用的顶点（仅被退化面引用也算孤立点）。
- **连通分量**：在有效面的顶点邻接关系（面内三点两两相连）上做并查集。
  每个分量统计面数、顶点数、边界边数、非流形边数、方向冲突数，以及
  `watertight`（无边界边且无非流形边）、`orientation_consistent`（无方向冲突）。

---

## 2. 构建

```bash
make            # 生成 build/gridtopo_check
make test       # 编译并运行 C++ 单元测试 + Python 端到端测试
make clean
```

编译选项：`-std=c++17 -O2 -Wall -Wextra -Wpedantic`。

---

## 3. 请求格式

```json
{
  "config": {
    "coordinateSystem": "right-handed Cartesian (x, y, z)",
    "lengthUnit": "metre",
    "eps": 1e-9,
    "areaEps": 1e-12
  },
  "vertices": [
    {"id": "v0", "coordinates": [0.0, 0.0, 0.0]},
    {"id": "v1", "coordinates": [1.0, 0.0, 0.0]},
    {"id": "v2", "coordinates": [0.0, 1.0, 0.0]}
  ],
  "faces": [
    {"id": "f0", "vertices": ["v0", "v1", "v2"]}
  ]
}
```

支持两种简写（可混用）：

- 顶点：`[x, y, z]`，或 `{"id": "...", "coordinates": [x,y,z]}`（坐标字段也接受
  `position`/`xyz`/`point`）。无 id 时自动命名 `v0, v1, …`。
- 面：`[i, j, k]`（顶点整数下标，0 起），或 `{"id": "...", "vertices": [...]}`
  （元素可用字符串 id 或整数下标；面字段也接受 `indices`/`triangles` 别名）。
  无 id 时自动命名 `f0, f1, …`。**面顶点顺序即绕序（朝向）。**

### 用法

```bash
./build/gridtopo_check samples/request_tetrahedron_closed.json
./build/gridtopo_check samples/request_tetrahedron_closed.json -o report.json
cat request.json | ./build/gridtopo_check
```

报告打印到标准输出（`-o` 额外写文件）。

### 退出码

| 码 | 含义 |
|---|---|
| 0 | 请求合法且网格无几何退化、无拓扑错误（开曲面也可返回 0） |
| 1 | 请求合法，但发现几何退化或拓扑错误 |
| 2 | 请求本身无法处理：JSON 语法错误、字段缺失/类型错误、顶点引用越界、重复 id 等（返回 `status:"request_error"`） |

---

## 4. 响应结构（关键字段）

- `status`：`valid` / `errors`
- `summary`：顶点/面总数、几何退化数、拓扑错误数、非流形边数、重复面组数/多余面数、
  方向冲突数、边界边数、孤立点数、连通分量数、`watertight`、`orientation_consistent`、`valid`
- `geometric_degeneracies[]` / `topological_errors[]`：每个问题含
  `type`、`message`、**`face_ids`（问题对应面 ID）**、`vertex_ids`、
  可选 `metric`（面积/距离/入射面数）与重复面的 `detail`
- `boundary_edges[]`：边界边端点 id 对
- `isolated_vertices[]`：孤立点 id
- `connected_components[]`：逐分量统计（见第 1 节）

问题类型一览：

| 类别 | type |
|---|---|
| geometric_degeneracy | `zero_area_triangle`、`repeated_vertex_index`、`coincident_vertices`、`nonfinite_coordinate`、`face_uses_nonfinite_vertex` |
| topological_error | `nonmanifold_edge`、`duplicate_face`、`isolated_vertex` |

---

## 5. 验收场景（samples/ 下有对应请求与已保存输出）

| 场景 | 请求文件 | 预期 |
|---|---|---|
| 封闭四面体（4 面） | `request_tetrahedron_closed.json` | exit 0；封闭、无边界边、1 个分量（4 面 4 点）、绕序一致 |
| 开洞曲面（平面环带 8 面） | `request_open_hole_surface.json` | exit 0；8 条边界边（外环 4 + 内洞 4）、不封闭但无错误、1 个分量 |
| 三面共边 | `request_three_faces_one_edge.json` | exit 1；`nonmanifold_edge` 命中边 (v0,v1)，`face_ids=["f0","f1","f2"]`，入射面数 3，6 条边界边 |

补充样例：`request_duplicate_face.json`（反向重复面）、
`request_orientation_conflict.json`（两面同向遍历共享边）、
`request_isolated_vertex.json`（孤立点）、
`request_geometric_degeneracy.json`（共线零面积 + 重合点，演示几何/拓扑分离）、
`request_bad_reference.json`（越界引用，exit 2）。

已保存报告见 `output/report_*.json`。

---

## 6. 自动化测试

```bash
make test
```

包含两层（无需联网、无第三方库）：

1. **C++ 断言单元测试** `tests/test_topology.cpp`（13 组）：封闭四面体、开洞、三面共边、
   反向/同向重复面、方向冲突、孤立点、多分量、零面积、重复顶点索引、重合点、
   非有限坐标、JSON id 往返。
2. **端到端测试** `tests/run_e2e.py`（10 个用例）：以真实子进程运行二进制，
   断言退出码、问题类型、面 ID、数值指标、stdin 管道与坏 JSON 行为。

---

## 7. 实际运行记录

在 Ubuntu 24.04、g++ 13.3.0、Python 3.12.3 上执行。完整日志见 `docs/run_log.txt`。

- `make`：编译通过，`-Wall -Wextra -Wpedantic` **无警告**。
- `make test`：
  - C++ 单元测试 **13/13 组全部 PASS**；
  - 端到端 **10/10 用例全部 PASS**。
- 退出码实测：

  | 请求 | 退出码 |
  |---|---|
  | request_tetrahedron_closed.json | 0 |
  | request_open_hole_surface.json | 0 |
  | request_three_faces_one_edge.json | 1 |
  | request_duplicate_face.json | 1 |
  | request_orientation_conflict.json | 1 |
  | request_isolated_vertex.json | 1 |
  | request_geometric_degeneracy.json | 1 |
  | request_bad_reference.json | 2 |

### 开发过程中实际出现并修复的问题（如实记录）

1. 初版 JSON 解析器有一处括号笔误导致编译失败，已修复。
2. `mesh.hpp` 误用 `const class json::Value&` 前向声明语法导致编译失败，
   改为直接包含 `json.hpp`。
3. 初版重复面绕序奇偶函数对“已排序副本”做索引定位，逻辑错误，
   导致 `[0,1,2]` 与 `[0,2,1]` 被误判为 `mixed_winding`；
   改为基于 `(顶点索引, 原位置)` 的置换奇偶后修正。
4. “2 个面同方向遍历一条边”最初单列为方向冲突类型；复核流形定义后
   将其归入**非流形边**（该边正反各一次的流形条件不满足），同时在
   `orientation_conflict_count` 中保留方向冲突计数。对应测试与样例注释同步更新。

当前**无未通过项**。

---

## 8. 目录结构

```
include/   json.hpp mesh.hpp topology.hpp
src/       json.cpp mesh.cpp topology.cpp main.cpp
tests/     test_topology.cpp run_e2e.py
samples/   request_*.json（验收与补充请求样例）
output/    report_*.json（实际运行保存的报告）
docs/      run_log.txt（构建与测试完整日志）
Makefile
```
