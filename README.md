# 空间最近邻索引（离线二维 KD 树后端）

纯后端、无前端的离线空间计算服务：读入一批二维点，构建**静态二维 KD 树**，
通过 **JSON 请求**执行 **K 近邻（KNN）**与**半径查询**，输出坐标与数值结果。
不含任何地图、渲染或可视化组件。

- 语言：C++17（仅标准库，无第三方依赖；自带极简 JSON 解析/序列化器）
- 平台：Linux（开发环境 Ubuntu 24.04, g++ 13.3, x86-64）
- 内容物：源码、构建脚本、请求/响应样例、自动化测试（含随机数据对照全扫描）

---

## 1. 构建与运行

```bash
make            # 产出 build/spatial_index 与 build/test_spatial

# 从文件读请求，结果写标准输出
./build/spatial_index examples/request_knn.json

# 从标准输入读，结果写到文件
cat examples/request_radius.json | ./build/spatial_index -o /tmp/out.json

make run        # 单元 + 随机对照测试
make e2e        # CLI/JSON 端到端测试（含 Python 独立全扫描交叉校验）
make asan       # AddressSanitizer + UndefinedBehaviorSanitizer 构建并测试
make check      # 以上全部
```

成功退出码 `0`；请求非法时输出 `{"ok":false,...}` 并以退出码 `1` 结束。

---

## 2. 坐标系、数值精度与度量约定

| 项目 | 约定 |
|---|---|
| 坐标系 | 二维笛卡尔平面直角坐标系（x 轴向右、y 轴向上，右手系）。**无量纲**，不做任何地图投影或大地测量换算（经纬度请由调用方先投影）。 |
| 距离度量 | 欧氏距离。内部统一比较**平方距离**（`distance_sq`），避免开方误差并减少计算；响应同时给出 `distance`（开方值）。 |
| 数值类型 | C++ `long double`（x86-64 Linux/gcc 上为 80 位扩展精度，约 18–19 位有效十进制数字）。 |
| 输入安全范围 | 坐标与半径必须是有限数且 `|v| ≤ 1e2400`。该上限保证任意两点平方距离仍有限（`(2·1e2400)² = 4e4800 < 1.79e4932`，即 long double 最大值）。超限请求被拒绝（`VALUE_OUT_OF_RANGE`）。 |
| 并列排序 | 结果一律按 `(平方距离升序, id 升序)` 排序——**距离相同时 id 更小者在前**。 |
| 边界包含 | 半径查询为闭区间：欧氏距离 **恰好等于** 半径的点也返回。 |

### 浮点边界与退化处理（重要）

- 判定全部基于平方距离的直接比较，不做任何 epsilon 容差。距离是否相等完全取决于
  long double 运算结果；当两点的数学距离差小于当前数量级的 ULP 时，会被视为等距，
  此时由 id 决定先后（这是确定性规则，而非随机结果）。
  - 例如坐标在 `1e150` 量级时，ULP 约 `1.08e131`，比这更小的坐标差不可区分。
- `k = 0` 返回空；`k > 点数 N` 时返回全部 N 个点，响应中 `truncated=true`。
- **重复坐标不去重**：视为不同点（id 不同），距离 0 的并列结果按 id 排序。
- **全部点坐标相同**时仍按 id 完成平衡划分（中位数划分的比较器以 id 收尾），
  树高保持 O(log N)，不会退化成链或撑爆栈（10 万重合点有专项测试）。
- 空点集、空查询列表均合法。
- id 为 int64 整数（允许负数），且点集内必须唯一；重复 id 直接报错。

---

## 3. 请求 / 响应格式

### 请求（JSON 对象）

```json
{
  "points": [
    {"id": 1, "x": 0.0, "y": 0.0},
    {"id": 2, "x": 3.0, "y": 4.0}
  ],
  "queries": [
    {"type": "knn",    "x": 0, "y": 0, "k": 3},
    {"type": "radius", "x": 0, "y": 0, "radius": 5.0}
  ]
}
```

- `points[].id`：int64 整数，点集内唯一。
- `points[].x/y`、`queries[].x/y`：数字，有限且 `|v| ≤ 1e2400`。
- knn：`k` 为非负整数。
- radius：`radius` 为 `[0, 1e2400]` 内有限数；负数/NaN 报错。
- 额外字段会被忽略（如样例中的 `_comment`）。

### 成功响应

```json
{
  "ok": true,
  "summary": {"points": 7, "queries": 3, "build_time_us": 120, "query_time_us": 18},
  "results": [
    {
      "type": "knn", "k": 3, "truncated": false, "count": 3,
      "results": [
        {"id": 1, "x": 0.0, "y": 0.0, "distance_sq": 0.0, "distance": 0.0}
      ]
    }
  ]
}
```

### 错误响应（HTTP 无关，退出码 1）

```json
{"ok": false, "error": {"code": "INVALID_TYPE", "message": "queries[0] \"k\" 必须是非负整数"}}
```

错误码：`INVALID_JSON`、`MISSING_FIELD`、`INVALID_TYPE`、`DUPLICATE_ID`、
`VALUE_OUT_OF_RANGE`。

更多样例见 `examples/`（`request_*.json` / `response_*.json`），其中
`request_degenerate.json` 集中演示重复坐标、共线、极大坐标、负 id。

---

## 4. KD 树实现与剪枝正确性

- **构建**（`src/spatial.cpp`）：按深度交替选择分割轴（偶数层 x、奇数层 y），
  `std::nth_element` 取中位数，树高 O(log N)，构建复杂度 O(N log N)。
  比较器：先比分割轴坐标，坐标相等时比 id（严格弱序、划分确定）。
- **KNN**：维护大小为 k 的最大堆，堆顶是当前最差候选；先递归查询点所在的近侧
  半空间，再决定是否进入远侧。
  远侧剪枝条件（近侧遍历后用**最新**最差距离判定）：

  ```
  仅当 分割平面距离²  >  堆顶最差距离²  时才可剪枝远侧
  ```

  即**取等号时必须进入远侧**——平面另一侧可能存在距离相同、但 id 更小的点。
  这是“距离相同按 id 排序”规则下避免漏点的关键。
- **半径查询**：远侧半空间任意点到查询点的距离至少为查询点到分割平面的距离，
  故当 `平面距离² > r²` 时剪枝；**等于时不剪枝**（圆周边界点可能落在远侧）。
- 坐标差平方溢出时按 +∞ 处理（比较语义安全，不会 UB）。

复杂度（平衡数据下）：构建 O(N log N)；KNN 期望 O(k log N)；
半径查询期望 O(log N + 结果数)。最坏情况（如共线/重合退化）为 O(N)，
但树仍平衡、递归深度 O(log N)。

---

## 5. 测试与验收

| 测试 | 内容 |
|---|---|
| `tests/test_spatial.cpp`（`make run`） | 36 项：并列 id 排序、三点等距、重复坐标（KNN 与半径0）、水平/垂直共线、k>N、k=0、空树、半径边界包含、负半径、负坐标、单点、远离查询；**17×17 网格边界扫描**（查询点落在分割平面/圆周上）；10 组共 **1830 次**随机查询（每次同时校验 KNN 与半径）对照全扫描（整数网格/共线/连续随机/1e100/1e150 尺度）；极大坐标专项；10 万点平衡与性能冒烟。 |
| `tests/run_e2e.sh`（`make e2e`） | 文件/stdin/`-o` 三条 IO 路径；样例结果核对；**40 组随机请求用 Python 独立实现的全扫描逐字段交叉校验**（kd 树与两边参照实现三方一致）；7 类非法输入错误码与退出码。 |
| `make asan` | 上述单元测试在 ASan+UBSan 下零报错。 |

**参照实现**：`spatial::bruteKNN` / `spatial::bruteRadius`（全扫描排序，逻辑刻意
直白）与 Python 端独立全扫描，KD 树结果必须与两者逐字段（id、坐标、平方距离、
顺序）完全一致。

### 实测结果（本机，2026-09-24）

```
make run   → 通过: 36，失败: 0；10 万点构建 ~40–55 ms，1000 次 k=10 查询 ~5–7 ms
make e2e   → 全部端到端测试通过；随机交叉校验: 40 个数据集, 失败 0
make asan  → 通过: 36，失败: 0（无 ASan/UBSan 报告）
```

性能数字随机器波动，仅作冒烟参考；仓库没有需要网络/服务的组件。

---

## 6. 文件结构

```
src/json.hpp          零依赖 JSON 解析/序列化（含 int64 识别、UTF-8、错误定位）
src/spatial.hpp       KD 树/朴素参照实现接口
src/spatial.cpp       KD 树构建、KNN、半径查询、剪枝、全扫描参照
src/main.cpp          CLI 与 JSON 请求入口、参数校验、计时
tests/test_spatial.cpp 单元 + 随机对照 + 退化/极大坐标/性能测试
tests/run_e2e.sh      端到端 + Python 交叉校验 + 错误用例
examples/             请求与响应样例（knn / radius / degenerate / error）
Makefile
```

## 7. 可移植性说明

- 代码只使用 C++17 标准库；在 MSVC 等平台上 `long double` 可能等于 64 位 double
  （约 15–17 位有效数字），精度与 1e2400 上限会相应不同，排序/剪枝逻辑不变。
- 未使用多线程；输入为一次性离线索引（点集不支持增量更新）。
