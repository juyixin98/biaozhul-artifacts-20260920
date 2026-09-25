# segi — 精确线段相交后端（Exact Segment Intersection）

纯 C++20 离线空间计算后端 + JSON 请求入口。输入整数坐标，输出**精确有理数**坐标与分类，
全程不使用浮点。**不包含任何地图、渲染或前端代码**——仅输出坐标与数值。

- 零外部依赖：自带任意精度整数（`BigInt`，base 2³²）、有理数（`Rat`）和极简 JSON
- 中间量（叉积/点积，量级可达坐标量级的平方，64 位坐标下必溢出 64 位）一律走 `BigInt`
- 四类关系穷举互斥：`disjoint` / `cross` / `endpoint_touch` / `collinear_overlap`
- 退化情形显式处理：零长线段（点）、共线仅一点相接、端点接触、平行分离
- 端点交换（`p↔q`）结果不变：几何分类与坐标与端点书写顺序无关

---

## 1. 坐标系、精度与约定（语义规范）

| 项目 | 约定 |
|---|---|
| 坐标系 | 二维**整数笛卡尔坐标系**（数学平面，无地图投影）。x 向右、y 向上仅为习惯；算法不依赖朝向 |
| 输入 | 端点坐标为任意大小**十进制整数**（JSON 整数字面量，拒绝小数/指数，避免任何隐式浮点） |
| 输出坐标 | 有理数 `{"num": 整数, "den": 正整数}`，**已约分、分母恒正**，以十进制文本原样输出，位数不限 |
| 精度 | 精确。所有判定基于整数叉积/点积符号，中间结果用任意精度整数，无四舍五入、无容差（epsilon-free） |
| 线段 | 闭区间 `[p, q]`，含两个端点；`p = q` 为零长线段（视为一个点） |
| 交点顺序 | 共线重叠输出 `start ≤ end`（先比 x 再比 y）；单点相交输出该点 |
| 端点来源标记 | `contact_on_a/contact_on_b` 取 `"p"`（第一端点）、`"q"`（第二端点）、`"i"`（线段内部） |

### 关系分类的精确定义

对两条闭线段 A、B 的交集 `A ∩ B`：

1. **disjoint**：交集为空。
2. **cross**：交集恰为一个点，且该点是 A、B 双方的**严格内部点**（两线在该点横穿，非端点）。
3. **endpoint_touch**：交集恰为一个点，且至少一方以**端点**接触（含 T 接、端点-端点、
   共线线段仅在一个端点相接、零长点落在线段上/端点上、两个零长点重合）。
4. **collinear_overlap**：两线共线且交集含无穷多个点（长度为正的一维区间重叠，包括完全重合）。

“cross”要求双方均为内部点；只要公共点是任意一方端点，即归为 `endpoint_touch`，
因此四类两两互斥且覆盖全部情况。

### 共线情形的参数化（退化处理核心）

共线时将四个端点投影到 A 的方向向量 u 上，参数 `k = dot(P−A.p, u)`（公共分母 `|u|²` 为正，
比较只需比较分子）。B 的参数区间与 `[0, |u|²]` 求交：区间不相交→`disjoint`；
交集退化为单点→`endpoint_touch`；交集长度为正→`collinear_overlap`，
区间端点经 `X = (A.p·|u|² + k·u)/|u|²` 还原为有理坐标。

---

## 2. 构建

需要支持 C++20 的编译器（验证环境：g++ 13.3.0，Ubuntu 24.04，x86_64）。

```bash
make            # 产出 build/segi-cli 与 build/segi-tests
```

无第三方库、无系统安装步骤。

---

## 3. JSON 请求 / 响应

### 请求

单条：

```json
{
  "a": {"p": {"x": 0, "y": 0}, "q": {"x": 3, "y": 1}},
  "b": {"p": {"x": 0, "y": 2}, "q": {"x": 2, "y": 0}}
}
```

批量：

```json
{ "queries": [ { "a": {...}, "b": {...} }, ... ] }
```

### 运行

```bash
./build/segi-cli -f examples/02-rational-cross.json   # 从文件
cat examples/01-cross.json | ./build/segi-cli         # 从 stdin
```

### 响应字段

- `relation`：上述四类之一
- `segment_a_zero_length` / `segment_b_zero_length`：是否零长
- `point`：cross / endpoint_touch 时的交点，坐标为 `{x:{num,den}, y:{num,den}}`
- `contact_on_a` / `contact_on_b`：endpoint_touch 时的接触位置（`p`/`q`/`i`）
- `overlap.start` / `overlap.end`：collinear_overlap 的有理区间端点
- disjoint 仅含关系与零长标记

上例（(0,0)-(3,1) 与 (0,2)-(2,0)）精确交于 (3/2, 1/2)：

```json
{
  "status": "ok",
  "result": {
    "relation": "cross",
    "segment_a_zero_length": false,
    "segment_b_zero_length": false,
    "point": { "x": {"num": 3, "den": 2}, "y": {"num": 1, "den": 2} }
  }
}
```

退出码：`0` 正常（几何上 disjoint 也算正常）；`1` 输入/JSON/协议错误（响应带
`status:"error"` 或 stderr 打印 JSON 错误）；`2` 内部错误。

---

## 4. 目录结构

```
include/segi/   bigint.hpp   任意精度有符号整数接口
                rational.hpp 有理数（约分、分母恒正）
                geometry.hpp 线段相交分类接口与结果类型
                json.hpp     极简 JSON 解析/输出
src/            上述模块实现 + main.cpp（CLI 入口）
tests/          test_segments.cpp  C++ 单元/性质测试
                crosscheck.py      Python Fraction 参考实现 + 随机对拍
examples/       请求样例（01..08）与 examples/output/ 下的实际输出
docs/ALGORITHM.md  算法与溢出分析
Makefile  README.md
```

---

## 5. 测试与验收

### 一键

```bash
make check    # = C++ 内嵌测试 + 4000 组随机对拍（种子 20260923）
```

### C++ 测试（`make test`）

- `BigInt`：解析/格式化往返（含 ±2⁶³ 与 40 位十进制数）、加减乘除模（截断式取余）、
  gcd、比较；`Rat`：约分、符号、四则、比较
- 固定几何用例：交叉、有理交点、T 接、端点-端点、共线包含/部分/重合/仅一点相接/有间隙、
  斜向与竖直共线
- **零长线段**：点在线段内部/端点/线外/延长线上、两个零长点重合与否
- **大坐标**：±(2⁶³−1) 量级（64 位乘积必溢出）以及 2¹⁰⁰、10⁴⁰ 量级坐标
- **端点交换**：4000 组随机（含强制零长）逐一验证 A/B 各自 p↔q 四种排列下分类与坐标不变

### 随机对拍（`tests/crosscheck.py`）

用 Python `fractions.Fraction` 独立实现同一分类规范（见该文件 `reference()`），
随机小坐标为主，并按比例注入强制共线、零长、±2⁶² 大坐标、10⁴⁰ 超 64 位坐标；
每组再展开 4 种端点排列，逐字段比较分类、有理坐标、接触标记、零长标记。

---

## 6. 实际运行记录

在本环境实际执行的命令与结果（完整输出见 `examples/output/`）：

| 命令 | 结果 |
|---|---|
| `make all` | 成功，零编译警告（`-Wall -Wextra -Wpedantic`） |
| `./build/segi-tests` | **C++ tests OK: 12093 checks passed** |
| `python3 tests/crosscheck.py --seed 20260923 --count 4000` | **16028 项比较全部一致**（disjoint 10036 / overlap 3212 / cross 2316 / touch 464） |
| 同脚本 `--seed 1 --count 3000` | **12028 项全部一致** |
| 同脚本 `--seed 987654321 --count 3000` | **12028 项全部一致** |
| ASan+UBSan 构建运行测试 | **12093 checks passed，无任何 sanitizer 报告** |
| 8 个 examples | 全部 rc=0，输出已保存 |

开发过程中发现并修复的两个真实缺陷（如实记录）：

1. **gcd 别名导致死循环**：`magSub(x, y, x)` 输出与输入别名，先清零了输入，
   结果永不收敛（测试挂起）。改为临时变量后修复，回归测试覆盖。
2. 对拍脚本初版参考实现把有理坐标 JSON 化成字符串、且对 dict 直接比较，已对齐为
   任意精度整数比较；修复后对拍全绿。

当前**无未通过项**。

---

## 7. 设计取舍

- **自带 BigInt 而非引入 GMP/Boost**：构建环境无 Boost 开发头文件，也不假设系统库；
  base 2³² 实现对本问题（输入几十位十进制、少量乘法/除法）性能充裕。
- **拒绝非整数 JSON 数字**：坐标是整数坐标系的语义前提；显式拒绝 `1.5`、`1e3`，
  避免“看似支持浮点、实则在解析处就丢精度”的陷阱。
- **不做容差判定**：精确算术下无需 epsilon；所有边界（恰好共线、恰好过端点）由符号判定确定归类。
