# rect_union — 扫描线矩形并面积/周长后端

纯后端、离线空间计算服务。给定一组**轴对齐**整数坐标矩形，返回其**并集**的
**面积**与**周长（外边界总长）**。无地图、无图形界面、无任何第三方依赖；
仅做坐标与数值计算，输入输出均为 JSON。

- 语言：C++20（仅 POSIX/Linux socket，HTTP 模式）
- 算法：坐标压缩 + 线段树扫描线，时间 O(n log n)，空间 O(n)
- 构建：`g++` + `make`，不需要任何外部库（自带极简 JSON 与单元测试框架）

---

## 1. 坐标系、单位与精度

| 项目 | 约定 |
|---|---|
| 坐标系 | 笛卡尔平面，**y 轴向上**（面积/周长结果与 y 方向无关，仅作语义声明） |
| 坐标类型 | **整数**（int64），请求中出现小数/指数一律拒绝（HTTP 400） |
| 坐标范围 | 每个坐标 ∈ [−10⁹, 10⁹]，单次最多 1,000,000 个矩形 |
| 区域语义 | 每个矩形是**半开**区域 `[x1, x2) × [y1, y2)`，要求 `x1 ≤ x2`、`y1 ≤ y2` |
| 面积单位 | 整数格平方单位；`[0,2)×[0,2)` 面积为 4 |
| 长度单位 | 整数格单位；上述矩形周长为 8 |
| 内部运算 | 宽度/长度用 int64，面积与边界累加用 **signed 128 位**（`__int128`） |
| 输出 | `area`、`perimeter` 为 JSON 整数（int64）；若理论结果超 int64 返回 422 |

坐标取整数时结果恒为整数，故无浮点误差。半开语义下两个沿整格边相邻的矩形
共享的是一条"左闭右开"的边界，它不属于任何一个矩形的内部，天然不产生面积；
边界计数也对共享缝做抵消（见下节）。

### 退化矩形处理

`x1 == x2` 或 `y1 == y2` 的矩形（竖直线段、水平线段、点）在半开语义下
**区域为空**，不含任何格点单元，既不贡献面积也不贡献边界。处理策略：

- **跳过**该矩形，不报错；
- 在响应 `ignored_rectangles` 中报告其 `index`（与可选 `id`）及原因；
- 即使退化矩形恰好落在其他矩形的边上，也不会改变结果（由 fuzz 对拍覆盖）。

`x1 > x2` 或 `y1 > y2` 属于**畸形输入**，直接 400 拒绝（不做静默翻转）。

### 重叠与相邻边不重复计数

周长定义为并集区域的拓扑边界（内部孔洞边界同样计入，例如环形）。
实现上对 x、y 两个方向各做一次扫描线：在每个事件坐标 p，设事件前截面为 A、
事件后截面为 B，则该处暴露的横向边界长度为对称差

  |B \\ A| + |A \\ B|

- 一个矩形结束、另一个沿同一截面开始（相邻）：该区间 A、B 都覆盖，
  对称差为 0，**缝不计**；
- 完全重叠/包含：内部覆盖不变，内部矩形边界不增加周长；
- 同一 x（或 y）上有任意多个开始/结束事件时，先处理全部开始边、再处理
  全部结束边，逐条区间在压缩线段树上的覆盖长度增量会望远镜式合并为上面的
  对称差，故多事件同坐标也不会重复计数。

---

## 2. 构建

```bash
make            # 生成 build/rect_union
make test       # 单元测试（内置框架，无需 GoogleTest）
make check      # 单元测试 + 黑盒集成测试（含 HTTP，需 curl；无 curl 自动跳过）
make clean
```

编译参数：`-std=c++20 -O2 -Wall -Wextra -Wpedantic -Wshadow -Wconversion
-Wsign-conversion`，主程序零告警。

### 实际运行记录（2026-09-24，Ubuntu 24.04，g++ 13.3.0）

- `make all`：成功，主程序零编译告警，产出 `build/rect_union`。
- `make check`：**全部通过** —— 单元测试 **41/41**（含 5000 例固定种子
  随机矩形对拍小网格参考、2 万矩形相邻条带压力用例），黑盒集成 **18/18**
  （CLI 样例、退化记账、5 类错误路径，以及 HTTP `/healthz`、`/union`、
  400/404/405）。无未通过项。
- 规模实测：10 万个随机矩形（坐标 0..10 万、宽高 1..500），
  `time ./build/rect_union --file /tmp/big.json` 实测约 **0.65 s**
  （area=4,649,018,071，perimeter=53,571,566；含进程启动与 JSON 解析）。
- 开发过程中出现并已修复的问题（保留记录，未掩盖）：
  1. 头文件里的扫描线函数最初漏标 `inline`，双翻译单元链接时 multiple
     definition —— 已加 `inline`。
  2. HTTP 错误响应的 raw string 内容含 `)"` 提前终止字面量导致编译失败 ——
     已改写该消息文本。
  3. 路由顺序曾使 `GET /nope` 返回 405 而非 404 —— 已改为先判路径再判方法。
  4. 两个测试/脚本人工期望值写错过（楼梯周长误写 19，实为 18；一个同 x
     事件组用例面积/周长误写 24/22，实为 18/20；负坐标重叠面积误写 28，
     实为 21），均由独立逐格参考发现后改正——算法本身未改动。
- 环境备注：手工联调 HTTP 时首选的 18099 端口被本机另一个无关服务占用
  （其响应头/中文报错与本服务明显不同），本程序正确地报
  `bind ... Address already in use` 并退出；改用空闲端口 39147 后全部正常。

---

## 3. 请求格式

### CLI

```bash
build/rect_union                      # 从 stdin 读一个 JSON 请求
build/rect_union --file samples/overlap.json
build/rect_union serve --host 0.0.0.0 --port 8080
```

请求体（`id` 可选，未知字段一律忽略）：

```json
{
  "rectangles": [
    {"x1": 0, "y1": 0, "x2": 2, "y2": 2, "id": "A"},
    {"x1": 1, "y1": 1, "x2": 3, "y2": 3}
  ]
}
```

成功响应（字段以稳定数值为主；对象键顺序不保证）：

```json
{
  "ok": true,
  "area": 7,
  "perimeter": 12,
  "rectangles_in": 2,
  "rectangles_used": 2,
  "ignored_rectangles": [],
  "units": {
    "coordinate_system": "cartesian, y-up, integer lattice",
    "area_unit": "square grid units",
    "length_unit": "grid units",
    "semantics": "half-open [x1,x2) x [y1,y2)"
  }
}
```

错误响应：

```json
{"ok": false, "error": {"code": "INVALID_RECTANGLE",
 "message": "rectangle at index 0: field \"x1\" must be an integer (fractions not allowed)"}}
```

CLI：成功退出码 0，请求类错误退出码 1，用法/文件错误退出码 2。

### HTTP（内置的简易 POSIX 服务，一连接一请求）

| 方法与路径 | 行为 |
|---|---|
| `POST /union` | 请求体即上面的 JSON；状态码 200/400/413/422 |
| `GET /healthz` | `200 {"ok":true,"status":"healthy"}` |
| 其它路径 | 404；`/union` 上的非 POST 为 405 |

请求体上限 16 MiB，接收超时 15 秒。该服务为便利入口，非生产级 HTTP 栈。

### 错误码

| code | HTTP | 含义 |
|---|---|---|
| `INVALID_JSON` | 400 | 请求体不是合法 JSON |
| `INVALID_REQUEST` | 400 | 顶层不是对象、缺 `rectangles`、类型不对 |
| `INVALID_RECTANGLE` | 400 | 某个矩形缺字段/非整数/越界/角点倒置 |
| `TOO_MANY_RECTANGLES` | 400 | 超过 1,000,000 个 |
| `PAYLOAD_TOO_LARGE` | 413 | 请求体超过 16 MiB |
| `RESULT_OVERFLOW` | 422 | 结果超出 int64 |

---

## 4. 目录结构

```
src/
  sweep.hpp       扫描线核心（坐标压缩 + 覆盖线段树，双方向扫描）
  json.hpp        零依赖 JSON 解析/序列化
  app.hpp/.cpp    请求校验、退化处理、指标编排
  main.cpp        CLI（stdin/--file）与内置 HTTP 服务
tests/
  test_sweep.cpp  算法单元测试 + 小网格逐格参考 + 5000 次随机对拍 + 压力
  test_app.cpp    JSON/请求层测试
  framework.hpp   内置最小单元测试框架（TEST/EXPECT_EQ/ASSERT_EQ）
  test_main.cpp   测试运行入口
  integration.sh  二进制黑盒测试（CLI 与 HTTP）
samples/          请求样例（含嵌套、相邻、退化、同 x 多事件、非法输入）
README.md
Makefile
```

### 小网格逐格参考验证

`tests/test_sweep.cpp` 中的 `gridReference()` 是与扫描线完全独立的参考实现：
在输入的整数包围盒内逐格判定该格左下角点 `(x,y)` 是否落在任一
`[x1,x2)×[y1,y2)` 中；面积=内格数，周长=分隔"内格/外格（含包围盒外）"的
单位边数。固定用例逐一覆盖嵌套、相邻（x/y 两个方向）、点接触、零面积、
同 x 多事件、孔洞等；`FuzzVsGridReference` 再用固定种子生成 5000 组
0..7 小网格随机矩形（含退化盒）做差分对拍。

---

## 5. 样例速查

| 样例文件 | 说明 | area | perimeter |
|---|---|---:|---:|
| `samples/minimal.json` | 单个 2×2 | 4 | 8 |
| `samples/overlap.json` | 三个正方形，部分重叠、点不接触 | 11 | 18 |
| `samples/adjacent_and_degenerate.json` | 两个相邻 2×2 + 3 个退化盒 | 8 | 12 |
| `samples/nested.json` | 三层嵌套 + 左侧相邻矩形 | 175 | 60 |
| `samples/same_x_events.json` | 5 个矩形多个同 x 事件组的楼梯形 | 16 | 18 |
| `samples/invalid_fraction.json` | 小数坐标，拒绝 | — | 400 |

---

## 6. 设计说明与边界

- 面积只做一次 x 方向扫描（截面覆盖长度 × 板宽积分）；周长需要两个方向。
- 退化矩形在构造事件时直接过滤；过滤逻辑两个方向一致。
- 重复的完全相同矩形：线段树覆盖计数支持任意叠加，结果只算一次。
- 周长包含内孔洞边界（"回"字形会同时数外边与内边）；这是并集区域的
  标准周长（边界的 1 维测度）。
- 负坐标通过坐标压缩处理，无原点/非负假设。
- 不做地图渲染、不做前端；所有输出仅为坐标语义下的数值与诊断信息。
