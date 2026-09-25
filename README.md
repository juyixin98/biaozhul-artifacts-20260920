# 矩形装箱下界（Rectangle Packing Lower Bounds）

纯后端 Python + NumPy 项目：求解**二维固定宽条带装箱问题（2D strip packing）**，
矩形**不允许旋转**。输出：

1. 无重叠、不出条带的可行布局（启发式，含逐件坐标）；
2. 启发式占用高度（一个**上界**，**不保证、也从未被声明为全局最优**）；
3. 可证明的**有效下界**（任何合法布局都必须 ≥ 它）。

不含任何前端。核心算法（几何、下界、启发式、精确穷举、碰撞检测）全部自行实现，
仅依赖 NumPy。

---

## 1. 问题定义

给定条带宽度 `W > 0` 和 `n` 个矩形 `(w_i, h_i)`（不旋转），把所有矩形
轴对齐、互不重叠地放进宽 `W` 的竖直条带，最小化占用高度 `H`。这是 NP 难问题。

本项目不宣称求全局最优（除非显式调用小规模精确求解器），而是同时给出：

* **启发式高度** `H_heur`：可行解，故 `OPT ≤ H_heur`；
* **有效下界** `LB`：`LB ≤ OPT`；
* 二者夹逼真实最优：`LB ≤ OPT ≤ H_heur`。

当 `H_heur == LB`（容差内）时，该算例的最优被**间接证明**；否则真实最优
可能落在区间内的某处，启发式结果**不能**被当作最优。

---

## 2. 目录结构

```
packing/
  geometry.py          几何原语、容差、向量化两两碰撞检测
  bounds.py            输入校验、范围限制、有效下界（面积/最大件高/宽件）
  heuristics.py        Bottom-Left（多排序）与 FFDH 货架启发式（非最优）
  exact.py             小整数算例的精确求解（子集和坐标 + 位掩码 DFS）
  grid_bruteforce.py  独立网格暴力法（仅测试中交叉验证精确求解器）
  layout.py            布局合法性校验（出界/负坐标/重叠）
  api.py               JSON 接口编排（纯函数，不绑 Web 框架）
  cli.py               命令行接口 python -m packing.cli
tests/                 unittest 自动化测试（51 个测试 + 子测试）
examples/              请求与响应样例（成功、精确、各类失败）
RUN_LOG.md             实际运行命令与结果记录（含曾失败的项）
```

---

## 3. 安装与运行

需要 Python 3.10+ 与 NumPy。

```bash
pip install -r requirements.txt        # 仅 NumPy
```

命令行（从文件或标准输入读取请求 JSON）：

```bash
python -m packing.cli examples/request_example.json
cat examples/request_example.json | python -m packing.cli
```

也可作为库调用：

```python
from packing.api import solve
resp, http_status = solve({"strip_width": 10,
                           "rectangles": [{"width": 6, "height": 4}]})
```

退出码：成功 `0`；输入不合法（`status=error`）`1`；
内部自检失败（`status=failed`）`2`；请求不是合法 JSON 或文件缺失 `3`。

---

## 4. JSON 接口

### 请求

```json
{
  "strip_width": 10,
  "rectangles": [
    {"width": 6, "height": 4},
    {"width": 5, "height": 3}
  ],
  "compute_exact": false
}
```

* `strip_width`（必填，正数）：条带宽度。
* `rectangles`（必填，非空数组）：`{width, height}` 列表；宽高顺序固定，**不旋转**。
* `compute_exact`（可选，默认 `false`）：对小整数算例额外求真实最优高度，
  仅用于验证/教学，见第 6 节。

### 成功响应（节选）

```json
{
  "status": "ok",
  "input_summary": {"strip_width": 10.0, "num_rectangles": 2},
  "lower_bounds": {"area": 3.9, "max_height": 4.0, "wide_items": 4.0, "combined": 4.0},
  "heuristic": {
    "height": 7.0,
    "method": "bottom-left/height-desc",
    "guarantee": "heuristic (not proven optimal)",
    "placements": [{"id": 0, "x": 0.0, "y": 0.0, "width": 6.0, "height": 4.0}]
  },
  "layout_verification": {"ok": true, "height": 7.0, "collisions": [],
                        "out_of_bounds": [], "negative_coordinates": []},
  "gap": {"heuristic_minus_lower_bound": 3.0, "proven_optimal": false}
}
```

字段语义：

* `lower_bounds`：三个独立的**有效下界**及其最大值 `combined`（见第 5 节）；
* `heuristic.height` / `placements`：启发式上界与无重叠布局坐标；`guarantee`
  字段显式声明其非最优；
* `layout_verification`：对返回布局独立做的碰撞/出界/负坐标校验；
* `gap.proven_optimal`：仅当启发式高度 == 组合下界时为 `true`（间接证明最优）。

### 失败响应与状态

```json
{"status": "error", "error": {"code": "rectangle_too_wide", "message": "..."}}
```

| status | HTTP 风格码 | 含义 |
|---|---|---|
| `ok` | 200 | 成功 |
| `error` | 400 | 输入不合法（见下错误码） |
| `failed` | 500 | 内部自检发现不变量被破坏（不应发生；返回而非静默给错答案） |

错误码（`error.code`，稳定可机读）：

| code | 触发条件 |
|---|---|
| `invalid_type` | 顶层/字段类型错 |
| `missing_field` | 缺 `strip_width` / `rectangles` |
| `bad_strip_width` | 条带宽 ≤ 0、NaN/Inf 或超上限 |
| `empty_rectangles` | 矩形列表为空 |
| `too_many_rectangles` | 矩形数超过 300 |
| `bad_rectangle_size` | 某矩形宽/高 ≤ 容差（**含零尺寸、负尺寸**）、NaN/Inf 或超上限 |
| `rectangle_too_wide` | 某矩形宽 > 条带宽（不旋转时放不下，**超宽拒绝**） |
| `area_too_large` | 单矩形面积超上限 |
| `invalid_json`（仅 CLI） | 请求体不是合法 JSON |

---

## 5. 有效下界（valid lower bounds）

对任意合法布局高度 `H` 都成立的界：

1. **面积下界** `LB_area = Σ(w_i h_i) / W`。
   证明：所有矩形互不重叠地位于 `W × H` 内 ⇒ `Σ面积 ≤ W·H`。
2. **最大件高界** `LB_h = max_i h_i`。每件矩形都必须完整放进高度 H。
3. **宽件界** `LB_wide = Σ_{i: w_i > W/2} h_i`。任意两个宽于 `W/2`
   的矩形水平投影必然正长度重叠（`w_i + w_j > W`），不能处于同一水平带，
   故其高度必须竖直累加。宽恰为 `W/2` 者用严格不等号排除（两件可以并排贴合）。

`combined = max(三者)` 仍是有效下界，且通常最紧。

**关键边界**：输出绝不把启发式高度称为最优。`examples/response_example.json`
即一个启发式非最优的真实例子：下界 8.4、启发式 11、真实最优 10（用精确求解器验证）。

---

## 6. 精确求解器（可选，仅小整数算例）

`compute_exact: true` 时，对满足下列全部条件的算例用 DFS 求**真实最优高度**：

* `n ≤ 8`；
* `strip_width` 与各矩形宽高均为整数且 `≤ 40`。

否则 `exact.status = "skipped"` 并给出原因，不影响主结果。搜索超过节点预算
（300 万）时同样保守返回 `skipped`，**绝不把未证实的结果称为最优**。

算法（`packing/exact.py`）：候选左下角坐标取矩形宽/高的**子集和坐标**
（完备：任何可行布局的每条内部竖/横边都可解释为一串矩形宽/高之和），
用位掩码网格（`W ≤ 40`，一行一个 64 位整数）做碰撞检测，配合同形状去重、
MRV（可放位置最少的形状先放）、记忆化与面积剪枝；可行性对高度单调，
用二分搜索找最小可行高度。开发中曾误用"每件必须有左/下支撑"和
"必须覆盖最低最左空格"两种剪枝，实测它们在允许空洞的条带装箱中会误删
可行解，已移除（详见 `RUN_LOG.md`）。

---

## 7. 数值容差与输入范围

* 容差 `EPS = 1e-9`：
  * 尺寸 `≤ EPS` 按零尺寸拒绝（覆盖 `0`、负数与极小浮点噪声）；
  * 两区间仅在有**正长度**交集时算重叠；共边（端点重合）允许贴合，不算碰撞；
  * 输出坐标四舍五入到 9 位小数，清理 `2.0000000000000004` 类噪声。
* 范围（`packing/bounds.py`）：
  * 矩形数量 `n ≤ 300`（小/中规模）；
  * 条带宽 `≤ 1e6`；单矩形宽/高 `≤ 1e6`；单矩形面积 `≤ 1e12`；
  * 拒绝 NaN / ±Inf；布尔值不接受为数字。

---

## 8. 启发式（非最优）

* **Bottom-Left（BL）**：矩形依次放到"尽可能低、再尽可能靠左"的合法位置；
  对 5 种排序（高降序、宽降序、面积降序、最长边降序、输入序）各跑一次。
* **FFD（Decreasing Height）**：高降序放入第一个放得下的水平货架，否则开新架。

取全部候选中高度最小者。复杂度 BL 约 O(n³)、FFDH O(n²)，在 n≤300 内秒级。
它们是可行上界，**可能次优**（第 5、9 节有真实反例）。

---

## 9. 测试

```bash
python -m pytest tests/ -q
# 或： python -m unittest discover -s tests -v
```

覆盖的验收点：

* **小例穷举对照**：精确 DFS 与一个**独立实现的网格暴力法**（逐整数格点、
  无共享代码路径）在两件矩形全组合、手工小例与 60 个随机小算例上最优高度一致；
* **零尺寸拒绝**：宽 0、高 0、负数、小于容差、零条带宽等；
* **超宽拒绝**：`w > W`（含严格超界），等宽与容差内超出允许；
* **面积下界**：数值正确性 + 40 个随机算例上 `LB ≤` 已知可行布局；
* **布局碰撞检测**：共边/共角不误报、正重叠必报，人为叠放/出界/负坐标被检出；
* **启发式非最优**：200 个随机小算例与真实最优比较，实证存在启发式次优案例，
  且启发式永不低于真实最优；
* JSON 接口结构与 CLI 退出码冒烟测试。

实际运行输出与过程中发现并修复的问题见 `RUN_LOG.md`。
