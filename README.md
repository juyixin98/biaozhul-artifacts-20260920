# 矩形装箱下界（2D Fixed-Width Strip Packing）

纯后端计算库 + JSON 命令行接口。矩形**固定方向、不旋转**，条带宽度 W 固定，
目标是最小化装箱高度。输出：

- 一份**无重叠、不超宽、不越界**的可行布局（多种启发式取最优者）；
- 该布局的高度（最优高度 H\* 的**上界**，不声明为全局最优）；
- 一个**严格下界** LB（可证明 ≤ H\*）；
- 上界−下界差距；小规模实例可选**分支定界精确求解**，此时可能把差距夹逼为 0。

纯 Python + NumPy，无前端、无网络组件。

---

## 1. 运行环境

- Python 3.12（3.9+ 应可运行，未逐一验证）
- NumPy 2.x
- 自动化测试只用标准库 `unittest`，**不需要 pytest**

安装依赖：

```bash
pip install -r requirements.txt
```

## 2. 快速开始

```bash
# stdin -> stdout
cat examples/request_example.json | python3 -m strip_packing.api

# 文件输入/输出
python3 -m strip_packing.api -i examples/request_medium.json -o /tmp/resp.json
```

请求（JSON 对象）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `strip_width` | number | 条带固定宽度 W，必填，有限正数 |
| `rectangles` | array | 矩形列表，每项 `{"id"?: any, "width": number, "height": number}`；`id` 缺省取序号；允许空列表 |
| `exact` | `"auto"` / true / false | 默认 `"auto"`：n≤6 时自动精确求解；`false` 仅启发式；`true` 强制精确（预算耗尽时如实返回 `limit_reached`） |
| `tolerance` | `{"atol": 1e-9, "rtol": 1e-9}` | 碰撞/边界判定容差，见 §4 |

响应（成功）：

```json
{
  "status": "ok",
  "strip_width": 10.0,
  "num_rectangles": 4,
  "lower_bound": 10.0,
  "lower_bounds": {"area": 9.2, "max_height": 6.0, "pairwise": 10.0},
  "heuristic_height": 10.0,
  "heuristic_name": "BL-area",
  "optimality": "not_claimed",
  "gap_upper_minus_lower": 0.0,
  "gap_ratio": 0.0,
  "exact": {"requested": true, "status": "optimal", "optimal": true,
            "nodes_explored": 0, "elapsed_sec": 0.000005,
            "node_limit": 2000000, "time_limit_sec": 10.0},
  "placements": [
    {"id": "A", "x": 0.0, "y": 0.0, "width": 6.0, "height": 6.0}
  ],
  "layout_verification": {"valid": true, "violations": []},
  "note": "heuristic_height 是最优高度的上界，不声明为全局最优；……"
}
```

- `optimality` 恒为 `"not_claimed"`；只有 `exact.status=="optimal"`
  且 `gap_upper_minus_lower==0` 时，上下界夹逼才构成最优性证明。
- `placements` 顺序与请求矩形顺序一致；坐标原点为条带左下角。

失败响应：

```json
{"status": "error", "error_code": "RECTANGLE_TOO_WIDE", "error": "…",
 "optimality": "not_claimed"}
```

退出码：`0` 成功；`1` 实例被拒绝（下表前 5 类错误）；`2` 请求不是合法 JSON / 结构错误。

## 3. 输入范围与失败状态（硬限制）

| 项 | 限制 | 违规错误码 |
|---|---|---|
| `strip_width` | 有限，`0 < W ≤ 1e6` | `INVALID_WIDTH` |
| 矩形宽/高 | 有限，`0 < w,h ≤ 1e6`（**零尺寸、负值、NaN、Inf 一律拒绝**） | `INVALID_RECTANGLE` |
| 矩形宽 vs 条带 | `w ≤ W`（不旋转，超宽无法放置，**超宽拒绝**） | `RECTANGLE_TOO_WIDE` |
| 矩形数量 | `0 ≤ n ≤ 200`（本项目仅支持小中规模） | `TOO_MANY_RECTANGLES` |
| `atol`/`rtol` | 有限，`0 ≤ tol ≤ 1e-3` | `INVALID_TOLERANCE` |
| 顶层结构 | 必须是 JSON 对象且含两必填字段 | `INVALID_JSON` |
| 内部一致性 | 启发式/精确布局未通过碰撞复核 | `LAYOUT_VERIFICATION_FAILED`（正常不会发生，属于防御性失败） |

精确求解的预算型失败状态（非错误，退出码仍为 0）：

- `status: "ok_exact_limit_reached"`，`exact.status: "limit_reached"`：
  分支定界触达节点预算（默认 2,000,000）或墙钟预算（默认 10 s）。
  此时返回的高度仍是**已验证可行**的启发式上界，响应明确标记
  `optimal: false`，绝不暗示最优。

## 4. 数值容差

合法布局中矩形可以共享边界。浮点运算会让本应恰好接触的边产生
1e-15 量级的"负间隙"。碰撞检测采用**绝对+相对**容差：

```
eps = atol + rtol * max(1, 参与比较的特征尺度)
```

- x 方向特征尺度 = 所有相关 x 坐标/宽度/右边界的最大绝对值；
- y 方向同理（含上沿）；
- 仅当两矩形 x、y 两方向的区间重叠量都**严格大于** eps 才判定碰撞。

默认 `atol=rtol=1e-9`。这意味着：共享边、角点接触合法；
真正 ≥1e-6 量级（在默认尺度下）的侵入一定被检出。
下界的数学判据（如 `w_i+w_j > W`）使用严格比较、**不**做容差放松，
避免下界被高估。

## 5. 算法

### 5.1 严格下界（`strip_packing/lower_bounds.py`）

设最优高度 H\*：

1. **面积下界** `L1 = Σ wᵢhᵢ / W ≤ H*`（总面积不超过 W·H\*）。
2. **最大高度** `L2 = max_i hᵢ ≤ H*`。
3. **两两下界** `L3 = max{hᵢ+hⱼ : wᵢ+wⱼ > W} ≤ H*`。
   若两矩形宽之和超过 W，不存在一条同时穿过二者的水平线
   （否则该线上占用宽度 >W），故二者垂直区间不相交，必须一上一下。

返回 `max(L1,L2,L3)`。300 个随机整数小例与网格穷举最优值对照，
未出现一次下界超过真实最优（测试内置该类检查）。

### 5.2 启发式上界（`strip_packing/heuristics.py`）

- **NFDH**（Next Fit Decreasing Height，高降序，封层后不复用）；
- **FFDH**（First Fit Decreasing Height，高降序，复用第一个可容纳的层）；
- **Bottom-Left**（候选点为 {0,已放矩形右沿}×{0,已放矩形上沿}，
  选最低再最左的无碰撞位置；n≤60 时分别按高/宽/面积降序各跑一次）。

所有变体的产出都由 `geometry.verify_layout` 独立做边界+碰撞复核后
才允许返回；取高度最小者。`examples/request_example.json`
（W=10，矩形 6×6、6×4、4×4、4×4）展示了启发式的差异：
NFDH 得 14，FFDH/BL 得 10，而严格下界也是 10 —— 直接说明
**启发式高度是上界、可能次优，本项目不声称其全局最优**。

### 5.3 精确求解（`strip_packing/exact.py`，小规模）

左下角候选点分支定界：每一步可选择任意未放置矩形，放到有限候选点
（坐标来自 {0}∪已放矩形右/上沿）。完备性依据：任何最优布局都可
通过"先向下、再向左滑到触边"变换为左-下稳定布局而不增高度，
其每个角点都在候选点集合内，且贴靠依赖构成 DAG，搜索可按拓扑序复现。

剪枝：当前上沿/全局下界/剩余矩形冲突对的局部下界 ≥ 已知最好值；
同尺寸矩形对称消除；节点与墙钟双预算。最优值或布局找到后再次做
碰撞复核。

## 6. 测试

```bash
python3 -m unittest discover -s tests -v
```

`tests/test_packing.py`（38 个测试）覆盖验收点：

- **穷举对照**：`tests/grid_oracle.py` 是与分支定界完全独立实现的
  整数网格位图穷举器；手工 6 例 + 12 个随机例（n≤6）上，
  分支定界最优高度与网格穷举最优值逐项相等；
- **零尺寸拒绝**：宽或高为 0/负/NaN/Inf 全部抛 `INVALID_RECTANGLE`；
- **超宽拒绝**：w>W 抛 `RECTANGLE_TOO_WIDE`，w=W 接受；
- **面积下界**：手算值正确，且下界 ≤ 已验证可构造高度；
- 300 例随机网格对照下界合法性（`tests/check_lower_bounds.py`，可复现）；
- **布局碰撞检测**：面积重叠被抓、共享边/角点接触放行、
  浮点 1e-15 负间隙放行、超宽/旋转（宽高互换）被抓；
- 启发式布局全部通过独立复核、且高度 ≥ 下界；
- JSON 接口往返、三类退出码（0/1/2）、`optimality:"not_claimed"`；
- 精确预算耗尽状态、空实例、200/201 矩形边界。

## 7. 实际运行记录

下列命令与输出均在本环境实际执行（Linux x86_64，Python 3.12.3，
NumPy 2.5.3；pytest 未安装，安装被环境权限策略拒绝，故使用
标准库 unittest）。

- `python3 -m unittest discover -s tests` → **38 tests OK**（约 0.6 s）。
- `python3 tests/check_lower_bounds.py 300 1` → **0 violations**（约 2.9 s）。
- `cat examples/request_example.json | python3 -m strip_packing.api`
  → LB=10（pairwise），启发式 10，exact=optimal，布局复核 valid，退出码 0。
- 超宽样例 → `RECTANGLE_TOO_WIDE`，退出码 1；
  零尺寸样例 → `INVALID_RECTANGLE`，退出码 1；
  非 JSON 输入 → `INVALID_JSON`，退出码 2。
- 200 个随机矩形（W=50）：LB=442.60，启发式高度 451，复核 valid，
  端到端墙钟约 2.6 s；201 个 → `TOO_MANY_RECTANGLES`；空实例 → 高度 0。
- n=12 且 `exact:true`：10 s 预算耗尽，`exact.status=limit_reached`，
  仍返回已验证可行的启发式布局并明确 `optimal:false`。
- 已知局限（如实记录）：纯 Python 分支定界吞吐约 2–5 万节点/秒，
  n≥8 的较"松"实例（宽度富裕、形状相近）可能在 10 s 内无法证明最优；
  因此自动精确阈值设为 n≤6。需要更大规模的精确值时，
  可把求解器换成基于列生成/DP 的专业实现，接口无需改动。
- 未通过项：无遗留失败测试。开发过程中出现并已修复的问题：
  启发式 Placement 初始未按输入顺序返回导致复核误报、
  FFDH 新层 y 坐标计算错误、FFDH 在非高度降序下复用层导致碰撞
  （已限定 FFDH/NFDH 仅按高度降序，符合标准定义）、
  精确搜索未找到更优完整布局时 `best_at=None` 的空指针
  （已改为按"证明了最优值"处理并继续使用启发式布局）。

## 8. 目录结构

```
strip_packing/
  __init__.py      公开接口
  geometry.py      容差、碰撞检测、布局复核
  validation.py    输入范围、错误码
  lower_bounds.py  三个严格下界
  heuristics.py    NFDH / FFDH / Bottom-Left
  exact.py         分支定界精确求解
  solver.py        求解编排（含 EXACT_AUTO_N 等预算常量）
  api.py           JSON CLI（python3 -m strip_packing.api）
tests/
  test_packing.py  unittest 测试（38 个）
  grid_oracle.py   独立网格穷举对照器（仅测试用）
examples/
  request_example.json   互锁小例（LB=上界=10）
  request_medium.json    中等规模、仅启发式
  request_too_wide.json  超宽拒绝
  request_zero_size.json 零尺寸拒绝
requirements.txt
```
