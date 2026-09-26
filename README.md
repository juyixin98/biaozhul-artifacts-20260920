# 二分匹配证书（Bipartite Matching Certificate）

纯后端 C++17 服务：输入二分图，计算**最大匹配**，并输出**最小顶点覆盖**作为
最优性证书（Kőnig 定理）。核心算法从零实现，**不调用任何现成求解器 / 第三方
库**；另提供朴素穷举参考实现用于小规模交叉验证。无前端。

---

## 1. 算法与证书

- **最大匹配**：Hopcroft–Karp 算法（BFS 分层 + 阻塞流 DFS），时间复杂度
  O(E·√V)。源码：`src/bipartite.cpp`。
- **最小顶点覆盖（最优性证书）**：在最终最大匹配上，从所有未匹配左点出发沿
  交错路径（非匹配边 L→R、匹配边 R→L）做 BFS，标记可达点。按 Kőnig 定理：

  > 最小顶点覆盖 =（不可达的左顶点）∪（可达的右顶点）

- **为什么这是可验证的证据**：任意顶点覆盖的大小都不小于任意匹配的大小
  （每条匹配边互不相交，各需一个不同覆盖点）。因此，只要独立检查
  1. 返回的匹配由真实边构成、无公共端点；
  2. 返回的覆盖碰到每一条边（可行性）；
  3. 覆盖大小 = 匹配大小；

  就同时证明了匹配最大、覆盖最小——无需信任算法内部。响应中的 `verification`
  字段即服务端做这三项检查的结果；测试套件用独立的 Python 穷举实现再次核对。

- **朴素参考**（`src/brute.cpp`，仅小规模）：
  - 最大匹配：左顶点回溯枚举全部匹配；
  - 最小顶点覆盖：枚举全部顶点子集，按定义直接判定。
  - 限界：左右顶点总数 ≤ 20、边数 ≤ 80（超过则返回结构化错误）。

原始顶点 ID（可为负数、稀疏、非连续，范围 ±2^53）在压缩索引后完整映射回
输出；重复边自动去重并在 `warnings` 中提示；孤立点可通过 `left` / `right`
显式声明（孤立点永远不出现在最小覆盖中）。

## 2. 规模限制

| 项目 | 上限 |
|---|---|
| 单侧顶点数 | 10 000 |
| 边数（去重后） | 200 000 |
| HTTP 请求体 | 16 MiB |
| 顶点 ID | [-2^53, 2^53] 整数 |
| 朴素穷举参考 | 总顶点 ≤ 20，边 ≤ 80 |

超限返回 `{"ok": false, "error": {"code": "LIMIT_EXCEEDED", ...}}`。

## 3. 构建

需要 g++（支持 C++17）与 GNU make，无任何外部依赖：

```bash
make            # 生成 ./build/bipartite-matching
make test       # 构建并运行自动化测试（另需 python3，仅标准库）
make clean
```

## 4. 请求 / 响应协议

### 请求（JSON 对象）

| 字段 | 类型 | 说明 |
|---|---|---|
| `left` | 整数数组（可选） | 左侧顶点 ID，用于声明孤立点 |
| `right` | 整数数组（可选） | 右侧顶点 ID |
| `edges` | 数组（可选，默认空） | `{"left":u,"right":v}` 或 `[u,v]`；重复边允许 |
| `brute_force` | 布尔（可选） | 是否附朴素穷举参考结果（受小规模限界） |
| `include_reachable` | 布尔（可选，默认 true） | 输出交错路径可达集 |

两侧 ID 集合必须不相交；声明了侧时，边端点必须属于所声明的一侧。
也可不给 `left`/`right`，此时两侧完全由边端点推断（无孤立点）。

### 响应（节选）

- `matching_size` / `matching`：最大匹配（原始 ID 对）；
- `minimum_vertex_cover_size` / `minimum_vertex_cover`：最小顶点覆盖
  （`{"side":"L"|"R","id":...}`，原始 ID）；
- `alternating_reachable`：Kőnig 构造中的可达点集（证书构造过程证据）；
- `verification`：`matching_is_valid`、`cover_touches_every_edge`、
  `cover_size_equals_matching_size`、`verified_optimal` 及文字论证；
- `brute_reference`（请求时）：穷举最大匹配数、穷举最小覆盖大小、`agrees`；
- `warnings`：如重复边/重复 ID 已去重；`elapsed_us`：求解耗时（微秒）。
- 错误：`{"ok": false, "error": {"code", "message"}}`，
  code 取值 `INVALID_JSON` / `INVALID_REQUEST` / `LIMIT_EXCEEDED` /
  `BRUTE_FORCE_TOO_LARGE`。

## 5. 使用方式

### stdin（单请求或 JSON 数组批处理）

```bash
./build/bipartite-matching < examples/request_basic.json
./build/bipartite-matching < examples/request_batch.json     # 数组 -> 数组
```

### 文件（JSON Lines，每行一个信封）

```bash
./build/bipartite-matching --file examples/request_basic.json \
    examples/request_empty_edges.json
```

### HTTP 服务

```bash
./build/bipartite-matching serve --host 127.0.0.1 --port 18080
curl -s http://127.0.0.1:18080/health
curl -s -X POST http://127.0.0.1:18080/solve \
  -H 'Content-Type: application/json' \
  -d '{"left":[1,2,3],"right":[4,5],"edges":[[1,4],[1,5],[2,4],[3,5]]}'
```

端点：`POST /solve`（JSON 请求体）、`GET /health`。
请求样例见 `examples/`（`examples/http_curl.sh` 为 curl 演示脚本），
已运行得到的响应存于 `examples/output/`。

## 6. 自动化测试

```bash
make test          # 或 python3 tests/run_tests.py
```

`tests/run_tests.py`（仅 Python 标准库）内置**独立的**朴素参考实现
（回溯枚举全部匹配；按基数递增枚举顶点子集求最小覆盖），覆盖：

- **350 个随机小图**：含孤立点、重复边、负数/稀疏 ID，逐一比对穷举结果，
  并检查覆盖碰到每条边、覆盖大小等于匹配数；
- 固定结构：K5,5、星形、C8 偶环、无边图、仅孤立点、隐式两侧、批处理；
- 错误与限界：非法 JSON、端点未声明、两侧 ID 重叠、非整数 ID、2^53 边界、
  穷举限界、200 001 边 / 10 001 顶点超限；
- CLI（stdin/`--file`/批处理）与 HTTP（200/400/404/405）端到端测试；
- 2000×2000 稀疏图性能冒烟测试。

## 7. 目录结构

```
Makefile
README.md
RUNLOG.md                  # 实际运行记录（命令、结果、未通过项）
src/
  json.hpp                 # 自带的最小 JSON 解析/序列化（无第三方依赖）
  bipartite.{hpp,cpp}      # Hopcroft-Karp + Kőnig 最小顶点覆盖
  brute.{hpp,cpp}          # 朴素穷举参考（小规模）
  protocol.{hpp,cpp}       # JSON 协议、限界校验、证书独立复核
  http_server.{hpp,cpp}    # POSIX socket 极简 HTTP/1.1 服务
  main.cpp                 # stdin / --file / serve 入口
tests/run_tests.py         # 自动化测试（含独立穷举参考）
examples/                  # 请求样例
examples/output/           # 实际运行保存的响应
```

## 8. 适用范围说明

这是本地使用的教学/验证型后端：HTTP 服务为单线程、无鉴权、无 TLS，
请勿直接暴露到公网。
