# sphdist — 球面距离查询（纯后端）

离线空间计算后端 + JSON 请求入口的 C++17 小项目。只做两件事：

1. **distance** — 计算两个经纬度点之间的球面大圆距离；
2. **range** — 在一组点中检索距某点不超过给定半径的点。

只输出坐标与数值，**无地图、无前端、无网络服务**（CLI 即全部入口）。
包围盒（bounding box）**只用于候选过滤**，最终判定一律用精确球面距离。

---

## 1. 构建与运行

依赖：g++（支持 C++17）、GNU Make、Python 3（仅集成测试需要）。无第三方库。

```bash
make            # 编译出 ./sphdist（-Wall -Wextra -Wpedantic 零警告）
make test       # C++ 单元测试 + Python CLI 集成测试
make run-examples  # 跑 examples/ 下全部请求样例
```

调用方式：

```bash
./sphdist examples/req_distance_antimeridian.json   # 文件参数
cat req.json | ./sphdist                            # 标准输入
```

退出码：`0` 成功；`1` 请求错误（400 类）；`2` 内部错误；`3` 用法/文件 IO 错误。
响应为 JSON，打印到 stdout。

---

## 2. 坐标系、地球模型与精度

| 项目 | 约定 |
|---|---|
| 输入坐标 | 地理经纬度（角度制），`lat ∈ [-90,90]`，`lon ∈ [-180,180]` |
| 经度边界 | `+180` 与 `-180` 视为同一经线，内部归一化为 `-180` |
| 高程 | 不支持，全部视为球面上的二维点 |
| 地球形状 | **球面模型**（不是 WGS84 椭球测地线） |
| 地球半径 | 固定 **R = 6 371 008.8 m**（IUGG 平均半径 R1），不随纬度变化 |
| 内部运算 | `long double`；JSON 数字以 IEEE-754 `double` 承载（约 15–17 位有效数字） |
| 输出 | 米（`distance_m`）与千米（`distance_km`），数字用 `%.17g` 往返无损 |

**精度说明**：double 经纬度本身的量化误差约为 1e-10°（赤道上约 0.1 mm），
距离计算的浮点误差远小于该量级（测试中与独立 Python haversine 的差异 < 2e-9 m）。
球面模型相对 WGS84 椭球测地线的**模型误差**可达 ~0.3%–0.5%（两极附近
球面距离偏小、赤道方向偏大），这是模型选择而非计算误差；若需要毫米级
大地测量结果应换用 Vincenty/Karney 测地线，不在本项目范围。

**关键常量（R = 6371008.8 m）**：

- 大圆 1°：`π/180·R = 111 195.080234 m`
- 四分之一大圆（赤道→极点）：`10 007 557.221018 m`
- 半周长（对跖点距离）：`πR = 20 015 114.442036 m`

---

## 3. 算法与退化处理

### 3.1 球面距离

经度差先归一化到 `[-180,180)`（保证跨反经线取**最短弧**），再用
haversine 半角式 + `atan2` 求中心角：

```
Δλ = wrap(lon2 − lon1)
a  = sin²(Δφ/2) + cos φ1 · cos φ2 · sin²(Δλ/2)
d  = R · 2 · atan2(√a, √(1−a))
```

不用 `acos(sinφ1 sinφ2 + cosφ1 cosφ2 cosΔλ)`：在对跖点附近自变量被
舍入到 ±1 时，`acos` 精度急剧恶化；`atan2` 形式在 0 和 π 两端都稳健。

退化情形：

| 情形 | 处理 |
|---|---|
| 两点重合 | 返回 0 |
| 同一极点但经度不同（如 `(90,10)` 与 `(90,−170)`） | 视为同一点，返回 0（极点经度无定义） |
| 对跖点（antipodal） | 返回 πR（半周长），不依赖 acos |
| `+180`/`−180` 同经线、跨反经线 | 经度差 wrap 到最短弧，2° 不会被算成 358° |
| 非法输入（越界、NaN/Inf） | 拒绝，返回结构化错误，不做静默容错 |

### 3.2 范围检索：包围盒只粗筛，距离终判

```
1. 以圆心、半径构造球面帽紧致包围盒（经纬度 AABB，可能跨反经线，最多拆成两块）
2. 用包围盒过滤候选点（candidate_count 仅用于观察过滤效果）
3. 对每个候选用 sphericalDistance() 精确复算，d <= radius 才入选
4. 结果按距离升序（距离相同按 id 稳定排序）
```

- **跨反经线**：盒内 `lon_min > lon_max` 表示跨越 ±180；命中条件为
  `lon >= lon_min || lon <= lon_max`。
- **盖极（近极点检索）**：球面帽覆盖极点时纬度方向触及 ±90°，
  经度方向退化为 `[-180,180]` 全宽——因为极点附近所有经度汇聚。
- 包围盒紧致经度半宽用 `asin(sinθ / cosφc)`，比直接取 θ 更紧。
- 单元测试对 **20 000 组随机查询**验证粗筛召回率 100%（真实命中绝不
  被候选阶段丢掉）；集成测试用独立 brute-force 枚举对照最终结果。

边界 `d == radius` 计入结果（闭区间）。

---

## 4. 请求 / 响应格式

### 4.1 distance 请求

```json
{
  "action": "distance",
  "a": {"lat": 0.0, "lon": 179.0},
  "b": {"lat": 0.0, "lon": -179.0}
}
```

响应：

```json
{
  "ok": true,
  "distance_m": 222390.16046706581,
  "distance_km": 222.39016046706581,
  "unit": "meter",
  "earth_radius_m": 6371008.8
}
```

### 4.2 range 请求

点集可内联（`points`）或引用外部文件（`points_file`，格式见
`data/points_world.json`：顶层 `{"points":[{id,lat,lon}, …]}`）。

```json
{
  "action": "range",
  "center": {"lat": 0.0, "lon": 180.0},
  "radius_m": 150000,
  "points_file": "data/points_world.json"
}
```

响应含 `total_points` / `candidate_count`（粗筛数）/ `match_count`
（距离终判数）、`candidate_bbox`（含 `crosses_antimeridian` 与拆分后的
`parts`）以及按距离排序的 `matches`。

### 4.3 错误响应

```json
{"ok": false, "error": {"code": "invalid_point",
                        "message": "a: latitude out of range [-90, 90]"}}
```

错误码：`invalid_json` / `missing_field` / `unknown_action` /
`invalid_point` / `invalid_radius` / `invalid_points` /
`invalid_points_file` / `points_file_error`。

---

## 5. 目录结构

```
src/                 生产源码
  geo.hpp/cpp        球面距离 + 球面帽包围盒（核心空间计算）
  json.hpp/cpp       自带的极简 JSON 解析/序列化（零依赖）
  service.hpp/cpp    请求解析、校验、distance/range 业务逻辑
  main.cpp           CLI 入口（文件参数或 stdin）
tests/
  unit_tests.cpp     C++ 单元测试（独立 acos 公式交叉验证、2 万组随机粗筛召回）
  test_cli.py        端到端 CLI 测试（独立 Python haversine/余弦定理对照）
data/points_world.json   示例数据集（含极点、±180、反经线两侧、高纬点）
examples/            请求样例（含正常与错误样例）与 run_all.sh
README.md            本文档
RUNLOG.md            实际构建/测试命令与结果的如实记录
Makefile
```

## 6. 范围外

不做：地图渲染、HTTP 服务、前端、椭球测地线、高程/三维距离、坐标参考系
转换、点集索引（大数据量可在当前接口内加网格/k-d 树，粗筛语义不变）。
