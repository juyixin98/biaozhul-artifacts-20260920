# Spatial Nearest-Neighbour Index (空间最近邻索引)

纯后端离线空间计算服务：静态二维 **KD 树**，支持 **K 近邻 (KNN)** 与 **半径查询**，
通过 JSON 文件/标准输入交互，仅输出坐标与数值，不含地图或前端。

## 1. 坐标系、距离与精度（先读）

| 项目 | 约定 |
|---|---|
| 平面 | 二维**笛卡尔平面**（不是经纬度，不做球面/大地距离计算） |
| 距离 | 欧氏距离，内部全程使用**平方距离** `d² = (x₁−x₂)² + (y₁−y₂)²`，只在输出时开方 |
| 数值类型 | 坐标解析为 IEEE-754 binary64 (`double`)，立即拓宽为 `long double`；平方距离与排序比较全部在 `long double` 中完成（x86-64 为 80 位扩展精度，64 位尾数） |
| 坐标范围 | 输入坐标仍受 binary64 表示范围约束（约 ±1.8e308）；平方距离中间量在约 ±1.18e4932 内不溢出。已实测坐标 ±1e15（对角差 2e15，d²=8e30）精确且不溢出 |
| 整数 ID | ID 必须是 int64 范围内的**整数且唯一**。解析时按原始 token 用 `strtoll` 精确转换，拒绝 `1.0`、`9007199254740993` 等无法精确表达的写法 |
| 输出精度 | 请求中的数字回显原 token；程序算出的数值按最短往返原则输出（binary64 用 17 位有效数字，扩展精度值用 21 位） |
| 非有限值 | `NaN`/`Infinity`/`1e999` 等在解析阶段拒绝 |

**相等距离的排序规则（全序）**：`(distance² 升序, id 升序)`。距离相等时一律以
ID 升序确定先后，结果不依赖堆顺序、插入顺序或内存布局。

**退化情形处理**：
- 重复坐标：允许，作为独立点分别索引；距离相等时按 ID 排。
- 共线/网格/全同点：建树按 `(分裂轴坐标, 另一轴坐标, id)` 的全序做 `nth_element`，
  即使坐标完全相同划分也是确定的。
- `k > 点数`：返回全部点；`k = 0` 返回空；空点集合法（返回空结果）。
- 半径查询为**闭球**：`d == r` 的点**包含**在内；负半径返回空。
- 极大坐标：见上表，平方距离中间量用 `long double` 避免 double 溢出。

**剪枝的边界处理（正确性要点）**：
- KNN：仅当分裂平面距离的平方**严格大于**当前第 k 近距离平方时才剪掉远侧；
  相等时远侧可能存在距离相等而 ID 更小的点，必须搜索。节点本身落在分裂平面上，
  在剪枝判定**之前**先评估。
- 半径：当且仅当 `平面距离² ≤ r²` 时搜索远侧（闭区间）。

## 2. 构建

需要 g++（支持 C++17，实测 g++ 13.3）与 GNU make。

```bash
make            # 生成 ./spatial_index
```

编译选项：`-std=c++17 -O2 -Wall -Wextra -Wpedantic -Wshadow -Wconversion -Wsign-conversion`（零警告）。

## 3. 使用

```bash
spatial_index [request.json | -] [response.json]
# 请求缺省或为 "-" 时从 stdin 读取；响应缺省输出到 stdout
```

### 请求

```json
{
  "points":  [ {"id": 1, "x": 0.0, "y": 0.0} ],
  "queries": [
    {"type": "knn",    "x": 0, "y": 0, "k": 3},
    {"type": "radius", "x": 0, "y": 0, "r": 5}
  ]
}
```

- `points`：必填。`id` 唯一 int64 整数；`x`、`y` 为有限数。坐标可重复。
- `queries`：必填（可为空数组）。
  - `knn`：`k` 为非负整数。
  - `radius`：`r` 为非负数；返回 `d ≤ r` 的全部点。

### 响应

成功：`{"ok": true, "indexed": N, "coordinate_system": {...}, "results": [...]}`，
每个邻居为 `{"id", "distance_squared", "distance"}`，按 `(distance², id)` 排序。
失败：`{"ok": false, "error": "..."}`，进程退出码为 1。

样例见 `examples/`：
- `request_basic.json` / `response_basic.json` — 等距环、k 超限、闭球边界
- `request_degenerate.json` / `response_degenerate.json` — 重复坐标、共线、±1e15
- `request_error_duplicate_id.json` — 重复 ID 的错误响应

快速试跑：

```bash
./spatial_index examples/request_basic.json | less
```

## 4. 自动化测试

```bash
make test
```

两层验证：

1. **C++ 单元测试** `tests/test_kdtree.cpp`（自包含断言，无第三方框架）：
   空集/单点、重复坐标、共线（横/竖 100+100 点）、等距环剪枝边界、±1e15 整数坐标、
   建树顺序无关性、40 组随机数据对全扫描（含 k=0、k=n、k>n、r=0、覆盖全部点的半径）。
2. **Python 差分测试** `tests/test_differential.py`：启动二进制，用 `Fraction`
   精确分数实现全扫描参照（整数坐标零误差），17 个场景，包括 2000 点 × 50 查询的
   大随机集、重复坐标、共线、网格对称、k 超限、极大坐标、以及非法 k / 重复 id / 非法
   查询类型等错误路径。

## 5. 实测记录（本机实际运行，非估算）

环境：Ubuntu 24.04，g++ 13.3.0，x86-64。

```text
$ make clean && make test
unit tests: 3266 passed, 0 failed
python3 tests/test_differential.py
PASSED 17  FAILED 0
```

性能抽查（`-O2`，含进程启动与 JSON 解析/序列化的墙钟时间）：

```text
100000 个均匀随机点（±1e9）建树 + 100 次 KNN(k=10) + 20 次半径查询：0.49 s
随后用 Python 全扫描对全部 120 个结果逐一比对：mismatches: 0
```

极大坐标抽查（见 `examples/request_degenerate.json`）：点 (±1e15, ±1e15) 与
查询 (0,0) 间 d² = 8e30，输出有限且 KNN 排序/ID 平局打破正确。

### 已知限制（如实说明）

- 坐标按 binary64 读入：十进制 `0.1` 等本身不被二进制精确表示，这是输入精度而非
  索引误差；`Fraction` 参照测试也按 binary64 取输入，故两者口径一致。
- 静态索引：一次性载入全部点并建树，不支持增量插入/删除。
- 仅二维笛卡尔欧氏距离；不支持经纬度/大地距离、维度参数化或其他距离度量。
- KD 树在高维（维度灾难）下退化，本项目明确只做二维。
- 无并发服务化封装（无 HTTP server），按题目要求仅提供 JSON 请求入口。

## 6. 目录结构

```
src/json.hpp        零依赖 JSON 解析/序列化（含整数 token 精确校验）
src/kdtree.hpp      静态二维 KD 树（KNN + 半径，边界正确剪枝）
src/main.cpp        JSON 请求入口、参数校验、批量查询
tests/test_kdtree.cpp      C++ 单元测试
tests/test_differential.py 对全扫描的差分测试
examples/           请求样例与真实响应
Makefile
```
