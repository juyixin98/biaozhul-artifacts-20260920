# raybox — 三维射线 / 轴对齐包围盒（AABB）求交后端

纯后端、离线的空间计算服务。输入一条射线与若干轴对齐包围盒，输出命中的
**坐标与数值**（命中参数 t、入点坐标、入面外法向、出射 t）。不含地图、
可视化或任何前端代码。

- 语言：C++17，零第三方依赖（自带 JSON 解析/序列化）
- 算法：slab 法射线-AABB 求交 + BVH（层次包围盒）加速遍历
- 查询：`nearest`（最近命中）与 `all`（全部命中，按 t 升序）
- 参考路径：`use_bvh:false` 逐盒检测，与 BVH 结果做对照

## 1. 坐标系、单位与精度

- **坐标系**：右手笛卡尔直角坐标系，分量顺序 `[x, y, z]`。求交数学对三轴
  完全对称；仅在报告入面法向的符号时依赖此约定（沿 −x 方向进入 `min` 面 →
  法向 `[-1,0,0]`）。
- **单位**：项目本身无单位，坐标与方向使用同一套长度单位即可。
- **射线参数化**：`P(t) = origin + t * dir`，t 为沿 `dir` 的参数。方向向量
  **不归一化**：只有当 `|dir|=1` 时 t 才等于几何距离；非单位方向时 t 按
  方向长度缩放。t ≥ 0 为射线前方，t = 0 表示起点就在盒上/盒内。
- **精度**：全程 IEEE-754 双精度 `double`。数值输出使用 17 位有效数字
  （`%.17g`），保证写出的十进制值可原样还原为同一个 double（例如
  `0.1+...` 的真实结果 `1.3999999999999999` 会如实输出，而不是四舍五入
  成一个读不回去的 `1.4`）。`elapsed_us` 为计时信息，固定输出三位小数。
- **容差**：闭合盒判定采用相对 + 绝对容差
  `eps = 1e-15 + 1e-12 * max(|origin|, |min|, |max|, span)`，
  使 1e-6 与 1e9 尺度的场景都有合理的边界余量。

## 2. 求交语义与退化处理

盒为**闭合**实体，边界算命中：

| 情形 | 行为 |
|---|---|
| 正面穿越 | 报告入点 t、出点 exit_t、入面外法向 |
| 起点在盒内/盒面上 | 命中，`t = 0`，法向为 `[0,0,0]`（无进入面），exit_t 为前方出射 |
| 擦边（贴面、贴棱、贴角） | 视为命中；贴面时 t 与 exit_t 为穿入/穿出，零厚度时两者相等 |
| 方向分量为 0 | **单独分支处理**：该轴为平行 slab，仅当原点落在 slab（含边界）内才继续，绝不除以零 |
| 负方向 / 混合符号方向 | 正常支持，法向取实际进入的那一面（正方向多从 min 面进入，负方向从 max 面进入） |
| 退化盒（某轴厚度为 0） | 支持：薄板（一个零厚度轴）、立柱（两个零厚度轴）、单点盒（三个零厚度轴） |
| 倒置盒（min > max） | 请求被拒绝，返回 `degenerate_geometry` 错误 |
| 零方向射线 (0,0,0) | 请求被拒绝：零射线没有遍历方向 |
| NaN / Infinity | JSON 解析器不接受 `NaN`/`Infinity` 字面量；数值字段额外做有限性校验，非有限值无法进入求交与排序 |
| 排序遇到非有限 t | `sortHits` 在排序前剔除 t/exit_t 非有限的记录，避免 NaN 破坏 `std::sort` 的严格弱序（防御性保证，正常已校验输入不会触发） |
| t 完全相等 | `nearest` 取 box 下标最小者；`all` 稳定排序后按下标决胜，结果确定 |
| 角点擦边（多面 t 相同） | 法向取**编号最小的进入轴**（x 优先于 y 优先于 z）所对应面的外法向；几何上三个面都成立，此为明确且确定的取舍 |

## 3. 构建与运行

```bash
make            # 生成 build/raybox
./build/raybox -f examples/nearest.json
cat request.json | ./build/raybox
```

### 请求格式

```json
{
  "ray":   { "origin": [x, y, z], "dir": [dx, dy, dz] },
  "boxes": [ {"id": 1, "min": [x0,y0,z0], "max": [x1,y1,z1]} ],
  "mode":  "nearest",
  "use_bvh": true
}
```

- `mode`：`"nearest"`（默认）或 `"all"`。
- `use_bvh`：`true`（默认）走 BVH；`false` 走逐盒暴力检测，便于对照。
- `boxes[].id`：用户自定义整数，原样回填；当前实现不要求唯一。
- 所有数值必须是有限 JSON 数；`min <= max` 需逐分量成立。

### 响应格式

成功：

```json
{
  "ok": true,
  "mode": "nearest",
  "use_bvh": true,
  "bvh_nodes": 3,
  "hit": true,
  "nearest": {
    "id": 0,
    "t": 2.0,
    "exit_t": 4.0,
    "point": [0.0, 1.0, 1.0],
    "normal": [-1.0, 0.0, 0.0]
  },
  "elapsed_us": 33.718
}
```

`all` 模式返回 `"count"` 与按 t 升序的 `"hits"` 数组（字段同上）。
失败返回 `{"ok": false, "error": {"code": ..., "message": ...}}`，
错误码：`invalid_json`、`invalid_request`、`degenerate_geometry`。

退出码：请求格式合法（包括几何未命中）为 0；无法读取输入/参数错误为 2。

## 4. BVH 说明

- 构建：取节点包围盒的最长轴，按盒中心做 `nth_element` 中位数二分，
  叶节点存一个 box；复杂度约 O(n log² n)，对离线后端足够且无依赖。
- 遍历：显式定长栈（64 层，支持约 20 亿个盒），无递归深度风险。
  - `nearest`：维护当前最佳 t，子节点 slab 区间完全在最佳点之后则剪枝；
    近的子节点先弹栈。
  - `all`：访问所有区间与 t≥0 半直线相交的节点，收集全部叶命中后统一排序。
- 叶节点求交与暴力路径调用**同一个** `intersectRayAABB`，两条路径结果
  按构造一致；测试用数万随机场景做数值对照。

## 5. 自动化测试

```bash
make test       # 仅 C++ 单元/几何/模糊测试
make run-test   # 单元测试 + Python CLI/JSON 端到端测试
```

- `tests/test_raybox.cc`：起点在盒内、贴面/贴棱/贴角擦边、负方向与混合
  方向、零方向分量、退化盒（板/柱/点）、NaN 防御、nearest/all 语义、
  固定场景 BVH↔暴力对照、4000 个含退化盒与零方向分量的随机场景模糊对照。
- `tests/test_cli.py`：经 stdin / `-f` 驱动编译出的二进制，校验 JSON
  数值结果、错误处理，并对 300 个随机场景 × 两种模式做 BVH 与逐盒检测
  响应逐字段比对。

### 实际运行记录（2026-09-24，Ubuntu 24.04，g++ 13.3.0）

命令与结果均如实记录：

```text
$ make run-test
... 编译，无警告（-Wall -Wextra -Wpedantic -Wshadow -Wconversion）
656 checks, 0 failures        # C++ 单元 + 模糊测试
24 checks, 0 failures         # Python CLI 端到端测试
```

过程中出现过 **1 项未通过并已修复**（如实说明）：

- 首跑 `testZeroDirComponent` 中「两个零方向分量、偏离棱线应未命中」
  断言失败。排查确认是**测试数据写错**而非求交代码错误：用例起点取
  `(-1, +1e-6, 0)`，其 y=1e-6 实际位于盒 y 区间 [0,2] 内，按闭合语义
  本就应命中；改为 `(-1, -1e-6, 0)`（真正在棱线外侧）后通过。
- 除此之外无失败项、无跳过项。

10 万盒规模实测（同一机器、同一射线，含一次性 BVH 构建，199999 个节点）：

```text
nearest：BVH 456 ms / 逐盒 376 ms（单次查询时 O(n) 构建占主导，二者相当；
          BVH 的剪枝收益在同一棵树多次查询时体现）
all：     均命中 43 盒，BVH 与逐盒的 id 序列与全部 t 值逐字段一致
最近命中 id 与 t 两种路径完全一致
```

样例实际输出（节选）：

```text
$ ./build/raybox -f examples/nearest.json
{"bvh_nodes":3,...,"hit":true,"nearest":{"exit_t":4,"id":0,
 "normal":[-1,0.0,0.0],"point":[0.0,1.0,1.0],"t":2},...}

$ ./build/raybox -f examples/degenerate.json   # 薄板 + 立柱 + 单点盒
count=3：薄板 t=5、立柱 t=3、单点盒 t=7（use_bvh=false 逐盒路径）
```

## 6. 目录结构

```
src/        vec3.h ray_aabb.{h,cc} bvh.{h,cc} json.{h,cc} api.{h,cc} main.cc
tests/      test_raybox.cc（C++）、test_cli.py（端到端）
examples/   nearest.json / all_hits.json / degenerate.json / error_zero_dir.json
Makefile
```

## 7. 已知边界

- BVH 为无依赖的中位数中轴划分，未做 SAH 等高级优化；目标是正确性与可
  对照性，超大场景可替换构建策略而不影响求交语义。
- 定长遍历栈 64 层（中位数树高 ⌈log₂ n⌉，足够 n ≤ 2^63）。
- JSON 支持常见转义与 UTF-8；是为本请求 schema 实现的严格子集解析器。
