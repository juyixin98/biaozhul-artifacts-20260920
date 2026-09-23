# meshcheck — 三角网格拓扑校验（纯后端）

离线三角网格拓扑检查器：读取 JSON 请求，执行空间/拓扑计算，输出坐标与数值结果（JSON 报告）。
**不含任何地图、渲染或前端组件。**

检测项：

| 类别 | 检测内容 | 输出字段 |
|---|---|---|
| 拓扑错误 | **非流形边**（同一条边被 ≥3 个面引用，即“三面共边”） | `errors.non_manifold_edges` |
| 拓扑错误 | **重复面**（三个顶点集合相同，忽略绕序与循环置换） | `errors.duplicate_faces`、`duplicate_face_groups` |
| 拓扑错误 | **方向冲突**（一条边恰好被 2 个面引用，但两半边绕向相同，无法形成一致定向） | `errors.orientation_conflicts` |
| 拓扑（信息） | **孤立点**（不被任何参与分析的面引用） | `errors.isolated_vertices` |
| 拓扑（信息） | **边界边/开洞**（仅被 1 个面引用的边） | `boundary_edges` |
| 连通性 | **连通分量统计**（面数、点数、边界边数、非流形边数、是否封闭） | `components` |
| 几何退化 | **零面积面**（共线、重复角点） | `geometric_degeneracies.degenerate_faces` |
| 几何退化 | **重合顶点**（距离 ≤ 容差的不同索引，合并并记录） | `geometric_degeneracies.duplicate_vertices` |
| 请求错误 | 越界索引、非整数索引、面不是三元组、坐标非有限数等 | `errors.request_errors` |

**几何退化与拓扑错误严格分离**：退化面与重复副本不参与半边/边的拓扑分析，
退化只记入 `geometric_degeneracies`，不会伪装成非流形或方向错误。

---

## 1. 坐标系、方向、精度与退化约定

- **坐标系**：输入三元组按**右手笛卡尔直角坐标系**解释，数值本身无量纲（README 与响应中标注为“米（局部、无基准）”）。
  程序不做任何大地基准/投影转换；只要全部坐标单位一致即可，容差与坐标同单位。
- **面绕序/法向**：采用右手定则，面 `[a,b,c]` 的法向为 `(Pb−Pa) × (Pc−Pa)`。
  两个相邻面正确配对时，其共享半边方向必须相反；方向相同即记为**方向冲突**。
- **精度**：全程 IEEE-754 双精度浮点（`double`），面积用叉积模长的 1/2 计算，
  数值结果以 `%.17g` 原样序列化，不做坐标舍入输出。
- **容差**：有效容差 `tol = eps_abs + eps_rel × 包围盒对角线长度`（默认均为 `1e-9`）。
  - 退化判定：面积 `≤ 0.5 × tol × 最长边`（即“高”不超过 tol），或出现重复角点；
  - 重合顶点：不同索引坐标距离 `≤ tol` 时合并为较小索引，记录 `[保留id, 被合并id]`（`--eps-rel 0` 关闭合并）。
- **退化处理**：
  - 点无效（不是 3 个有限数）：该点报错，引用它的面被拒绝；
  - 面索引越界/非整数/不是三元组：请求错误并拒绝该面；
  - 零面积面：接受但标记退化，**排除出拓扑分析**；只被它引用的点记为孤立点；
  - 重复面：保留每组第一份参与分析，其余标记为重复面；
  - 非有限数（`NaN`/`Infinity`，解析器宽容接受其记号）在点校验阶段以“必须为有限数”拒绝。
- **封闭（closed / watertight）的定义只基于边的入射次数**：分量内每条边恰好被 2 个面引用即为封闭，
  与绕向是否一致无关；因此“封闭但不定向”（翻转面）是可报告的状态。`orientable` 单独反映绕向一致性。

## 2. 构建与运行

依赖：g++（C++17）、GNU make、python3（仅测试驱动用）。无第三方库。

```bash
make            # 生成 build/meshcheck
make test       # 构建并运行自动化测试
```

```bash
# 从文件读取
./build/meshcheck examples/closed_tetrahedron.json

# 从标准输入读取
cat examples/closed_tetrahedron.json | ./build/meshcheck

# 选项
./build/meshcheck --eps-abs 1e-12 --eps-rel 1e-11 request.json
./build/meshcheck --compact request.json      # 紧凑 JSON
./build/meshcheck --help
```

退出码：

| 码 | 含义 |
|---|---|
| 0 | 报告已生成，且 `ok=true`（无请求错误、无拓扑错误类别问题；几何退化与开洞不算失败） |
| 1 | 报告已生成，但存在请求错误或拓扑错误（非流形边/方向冲突/重复面） |
| 2 | 无法生成报告（文件打不开、JSON 语法无法解析、根不是对象） |

> 说明：开洞（边界边）、孤立点、退化属于需要呈现但不必然“不合格”的信息，
> 因此不单独把退出码置为 1；判定结论以报告中的 `ok`/`summary` 与各列表为准。

## 3. 请求格式

```json
{
  "name": "可选，仅作标注",
  "points": [[x, y, z], ...],
  "faces":  [
    [0, 1, 2],
    {"id": 10, "verts": [0, 1, 2]}
  ]
}
```

- `points`：必填，三维有限数数组；
- `faces`：必填，元素可为三元索引数组，或对象 `{"id": <整数>, "verts": [a,b,c]}`
  （`vertices`/`indices` 作为 `verts` 的别名也可接受）。
- 面的对外 ID：对象形式用其 `id`；裸数组形式用其在 `faces` 中的下标（从 0 起）。

## 4. 响应结构（摘要）

```
ok                          总体布尔结论
coordinate_system           坐标系/容差/算术回显
summary                     计数与 manifold/orientable/closed 结论
errors.request_errors       请求级问题（字符串）
errors.non_manifold_edges   [{edge:[a,b], face_ids:[...], component}]
errors.orientation_conflicts 同上结构
errors.duplicate_faces      [面id ...]（冗余副本）
errors.isolated_vertices    [点id ...]
boundary_edges              [同上结构]（开洞信息，非错误）
geometric_degeneracies      {degenerate_faces:[...], duplicate_vertices:[[keep,drop],...]}
duplicate_face_groups       每组全部面 id（含保留的第一份）
components                  [{component, face_count, vertex_count,
                             boundary_edges, non_manifold_edges, closed}]
```

## 5. 验收样例

样例位于 `examples/`，对应响应保存在 `examples/responses/`。

| 样例 | 场景 | 期望结果 |
|---|---|---|
| `closed_tetrahedron.json` | 封闭正四面体、绕序一致 | 1 个封闭分量，F=4/V=4，0 错误，exit 0 |
| `open_fan_surface.json` | 扇形开洞曲面 + 孤立点 | 1 分量、9 条边界边、孤立点 9、不封闭 |
| `three_fans_non_manifold.json` | 三个面共享边 [0,1] + 独立三角片 | 非流形边 [0,1]，面 id **1,2,3**；2 个连通分量；孤立点 8；exit 1 |
| `flipped_face_tetrahedron.json` | 四面体中一个面绕反 | 封闭但 3 条边方向冲突（均涉及面 3），`orientable=false` |
| `degenerate_and_duplicate.json` | 共线零面积面、重复角点、重合点 | 退化面 11/12、合并点 [1,4]，与拓扑错误分离 |

## 6. 自动化测试

`tests/run_tests.py` 构造 10 组请求、调用二进制、解析报告并断言面 ID 与分量统计，
共 65 项断言。请求/响应产物落盘在 `tests/out/`。覆盖：

封闭四面体（含欧拉示性数 V−E+F=2）、开洞曲面、三面共边非流形、重复面（同向/循环置换）、
翻转面方向冲突、零面积退化、重合点合并、越界/错误元数/NaN 请求、stdin 与坏 JSON 退出码、
两个不相交封闭体的 2 分量统计。

## 7. 目录结构

```
src/json.hpp        零依赖 JSON 解析/序列化
src/mesh_check.hpp  检查器接口与数据结构
src/mesh_check.cpp  拓扑/几何检查核心
src/main.cpp        CLI 入口
examples/           请求样例
examples/responses/ 样例实际响应（随仓库保存）
tests/run_tests.py  自动化测试
Makefile
```

## 8. 已知限制

- 重合顶点合并为 O(n²) 两两比较，面向验收规模（数百点内）；大规模数据应替换为空间哈希。
- 面 ID 以 JSON number 输出；超出 2^53 的整数 id 会有双精度精度风险（常规索引不涉及）。
- 仅处理三角面；非三角面按请求错误拒绝，不做扇形化。
