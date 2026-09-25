# 精确线段相交（纯后端）

离线空间计算后端：读入一批**整数坐标**线段对，输出相交分类与**精确有理交点**。
不包含任何前端、地图或可视化组件；输出只有坐标与数值（JSON）。

- 语言：C++17（`__int128` 定向测试 + Boost.Multiprecision `cpp_int` 任意精度有理交点）
- 入口：命令行程序 `seginter`，JSON in / JSON out，支持 stdin 或文件
- 无浮点数参与任何判定与输出，从根本上消除精度误差

---

## 1. 坐标系、精度模型与退化约定

### 1.1 坐标系

- 二维笛卡尔直角坐标系，x 轴向右、y 轴向上，单位任意（抽象数学平面）。
- **不是**地理经纬度，不做任何投影或基准面换算。
- 所有输入坐标必须是 64 位有符号整数，且满足
  `|x|, |y| ≤ 2^62 − 2`（常量 `segint::MAX_COORD`）。
  超出范围的条目在入口层被拒绝并返回 `error`（不影响其它条目）。

### 1.2 精度模型（如何避免中间运算溢出）

| 运算 | 使用的类型 | 溢出论证 |
|---|---|---|
| 定向测试 `cross(q−p, r−p)` | 有符号 `__int128` | 坐标差 `|Δ| ≤ 2·MAX_COORD+1 < 2^63`；两项乘积 `< 2^126`，其差 `< 2^127`，落在 `__int128` 范围内 |
| 跨立判断 | 叉积的**符号比较** | 不计算两侧叉积的乘积（该乘积可达 2^252），只用正负号/零判断，杜绝溢出 |
| 交点坐标 | `boost::multiprecision::cpp_int`（任意精度） | 分子可达约 `2^126`，128 位边界附近已不安全，故坐标构造全部走任意精度整数，再约分为最简分数 |

输出坐标为有理分数 `{"num": "...", "den": "..."}`（十进制字符串，因为可能超出 int64），
**分母恒为正**，分数已约为最简，`0` 表示为 `0/1`。全流程不使用 `float/double`。

### 1.3 分类语义（四类，互斥）

| classification | 含义 |
|---|---|
| `none` | 不相交。包括延长线相交但线段不相交、平行/共线但无公共点 |
| `cross` | 恰有一个公共点，且位于两条线段的**内部**（真交叉） |
| `touch` | 恰有一个公共点，且至少涉及一个端点；**零长线段的接触也归此类** |
| `overlap` | 两条非退化线段共线且共享一段**长度为正**的区间；共线但仅端点相触归 `touch` |

### 1.4 退化处理

- **零长线段**（`a == b` 或 `c == d`）视为一个点：点在另一线段上（含端点）→ `touch`，否则 `none`。
- `hits` 字段列出坐标等于该接触点的**全部**端点名（`a/b/c/d`，按字母序）：
  例如零长线段 `a==b` 落在 `cd` 内部时为 `["a","b"]`；两个零长线段重合为 `["a","b","c","d"]`。
- 交点恰为端点时（T 字、角对角等）归 `touch`，并在 `hits` 中给出是哪个端点。
- `overlap` 返回重叠区间的两个端点，**按坐标升序排列**（与端点书写方向无关）。

### 1.5 对称性保证

对交换 `a↔b`、`c↔d` 以及交换两条线段，分类与点坐标不变；`hits` 标签随端点重命名而相应变化
（这是标签语义，不是几何差异）。测试中对每个随机用例的 4 种交换变体都做了校验。

---

## 2. 构建

依赖：

- g++（支持 C++17 与 `__int128`；实测 g++ 13.3.0）
- Boost 头文件（仅 `boost/multiprecision/cpp_int.hpp`，header-only）：
  Ubuntu/Debian 安装 `sudo apt-get install libboost-dev`
- 对拍测试另需 python3（标准库即可，`fractions.Fraction`）

```bash
make            # 生成可执行文件 ./seginter
make clean      # 清理
```

---

## 3. 请求 / 响应格式

### 用法

```bash
./seginter [request.json] [-o response.json] [--pretty]
# 省略输入文件 -> 从 stdin 读；省略 -o -> 输出到 stdout
# --pretty 以 2 空格缩进美化输出
```

### 请求

顶层对象，条目数组键名为 `entries`（也接受 `requests`）：

```json
{
  "entries": [
    {
      "id": 1,
      "a": {"x": 0, "y": 0},
      "b": {"x": 4, "y": 4},
      "c": {"x": 0, "y": 4},
      "d": {"x": 4, "y": 0}
    }
  ]
}
```

- 坐标只接受**整数**（解析器拒绝小数/指数形式）；`id` 可省略，或为整数/字符串。
- 单条错误（缺字段、类型错、坐标超界）只让该条返回 `{"id": ..., "error": "..."}`，其余照常计算。
- 顶层 JSON 整体解析失败时返回 `{"error": "..."}`。

### 响应

```json
{
  "results": [
    {
      "id": 1,
      "classification": "cross",
      "points": [
        {"x": {"num": "2", "den": "1"},
         "y": {"num": "2", "den": "1"},
         "hits": []}
      ]
    }
  ]
}
```

- `points`：`cross`/`touch` 恰一个点；`overlap` 两个点（坐标升序）；`none` 为空数组。
- 分数坐标以十进制字符串给出，`den > 0`，已约分。
- `hits`：接触点上的端点名（字母序）；真交叉时为 `[]`。

样例见 `samples/`：`request_basic.json`（基本分类）、`request_edge_cases.json`
（零长线段、大坐标、近平行、错误输入），对应的 `response_*.json` 为实际运行产物。

---

## 4. 测试

```bash
make test               # C++ 单元测试（确定性用例）
make random-test        # 随机小坐标 × Python 有理数参考实现对拍
make random-test-large  # 随机大坐标（接近 2^62 上界）对拍
make check              # 依次运行以上全部
make samples            # 用 samples/ 下的请求实际跑一遍，生成 response_*.json
```

### C++ 单元测试（`tests/unit_tests.cpp`）

覆盖：整数/分数交点真交叉、T 字与端对端接触、共线部分重叠/包含/全等/仅端点相触/分离、
零长线段全部退化分支、`2^62` 量级大坐标交叉/近平行交叉（叉积绝对值很小）、
端点交换几何不变性、JSON 解析边界。

### 随机对拍（`tests/random_compare.py`）

参考实现用 `fractions.Fraction` 精确运算、以独立的参数法
（求参数 `s,t ∈ [0,1]`，共线时做点积投影）独立编写，不共享 C++ 的判定结构。
每个随机用例展开为原样、交换 ab、交换 cd、线段对互换 **4 个变体**，
逐条比较分类、点坐标（精确分数相等）与 `hits`。

### 实测结果（本机，g++ 13.3.0，Ubuntu 24.04 内核 6.8）

```
$ make test
checks: 240, failures: 0
UNIT TESTS PASSED

$ make random-test         # --count 20000 --coord-range 8 --seed 20260923
cases compared: 80024, mismatches: 0
RANDOM COMPARISON PASSED

$ make random-test-large   # --count 5000  --coord-range 4611686018427387902 --seed 987654321
cases compared: 20024, mismatches: 0
RANDOM COMPARISON PASSED
```

合计约 **10 万个**端点交换变体与参考实现完全一致，无未通过项。
大坐标近平行交叉的分数坐标另与 Python 手算参考逐一核对一致
（例：`y = 9223372036854775804/13835058055282163705 = 2s`，精确无舍入）。

---

## 5. 目录结构

```
src/
  geometry.{hpp,cpp}   线段相交判定、分类、精确有理交点
  rational.hpp         任意精度最简有理数（cpp_int）
  json.{hpp,cpp}       最小整数 JSON 解析/序列化
  main.cpp             命令行 JSON 入口
tests/
  unit_tests.cpp       C++ 单元测试
  random_compare.py    Python 有理数参考实现 + 随机对拍
samples/
  request_basic.json / request_edge_cases.json
  response_*.json      实际运行输出
Makefile
README.md
```

## 6. 算法说明（简要）

对非退化线段 `ab`、`cd`，记叉积：
`den = (b−a)×(d−c)`，以及四个定向值
`orient(a,b,c)`、`orient(a,b,d)`、`orient(c,d,a)`、`orient(c,d,b)`。

- `den ≠ 0`：两线交于唯一点。用两侧定向值的符号（含零）判断是否跨立；
  四个定向值均非零且成对异号 → `cross`；某定向值为 0 且点在线段上 → `touch`；
  否则 `none`。交点参数 `t = ((c−a)×(d−c))/den`，代入 `p = a + t(b−a)`，
  以 `cpp_int` 算分子分母并约分。
- `den = 0`：平行；若 `orient(a,b,c) ≠ 0` 则不共线 → `none`。
  共线时在非退化投影轴（竖直线段取 y，否则取 x）上做闭区间一维相交：
  无交集 → `none`；退化为一点 → `touch`；长度为正 → `overlap`。
- 任一线段零长时退化为“点在线段上”判定（共线 + 包围盒包含）。
