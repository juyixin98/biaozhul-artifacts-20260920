# 增量拓扑排序（Incremental Topological Sort）后端

纯后端 C++17 项目：维护一个有向图，在**逐条插入边**时增量维护其拓扑顺序；
若新边会形成环，则**拒绝插入并返回一条具体的环路径**，且失败插入不改变图。
支持自环检测与重复边（重复边幂等、零搜索开销）。核心求解**不调用任何现成
图/求解器库**；另提供一个朴素的"每次插入后全量 Kahn 重算"小规模参考实现，
用于差分测试与性能对照。

无前端，无第三方依赖（JSON 解析/序列化为自带的最小实现 `src/json.hpp`）。

---

## 1. 算法说明

边 `u -> v` 表示 `u` 必须排在 `v` 前面。维护一个全序，使每条已提交边满足
`order(u) < order(v)`。

### 增量算法（Pearce–Kelly 有界双向搜索，"PK"）

插入边 `u -> v` 时（参考：Pearce & Kelly, *A Dynamic Topological Sort Algorithm
for DAGs*, ACM JEA 11, 2006）：

1. `u == v`：自环，直接拒绝，见证路径 `[u, u]`。
2. 边已存在：幂等接受，标记 `duplicate`，不做任何搜索或移动。
3. 若当前 `u` 已在 `v` 之前：边与序一致，直接提交（`reordered=false`）。
4. 否则在位置窗口 `[v, u]` 内做**双向有界搜索**：
   - 前向搜索：从 `v` 沿出边扩展其下游集合 δF；
   - 后向搜索：从 `u` 沿入边扩展其上游集合 δB；
   - 两侧交替地总是扩展当前较小的集合，且都不越出位置窗口；
   - 两侧相遇 ⇒ 已存在路径 `v →* u`，新边将成环，**拒绝且不改图**，
     并用一次 BFS 复原具体环 `[u, v, …, u]`；
   - 一侧先穷尽 ⇒ 无环，把**较小**的那个集合在窗口内做稳定移动
     （δF 移到窗口高端 / δB 移到窗口低端），再提交边。

### 顺序的表示（相对教科书数组版的工程改进）

PK 的局部重排本质是对位置窗口做一次**稳定划分**（"把访问到的点整体移到窗口
一端"）。

- 用普通数组：即使只访问了 2 个点，也要物化整个窗口，随机稀疏流下窗口平均
  约 n/3，单次插入退化到 O(n)。
- 用平衡树（本项目开发中试过隐式 Treap）：重排变快，但搜索里"是否在窗口内"
  退化成每次邻接判断一次 O(log n) 的排名查询，随机流下反而更慢。

最终采用**带 64 位间隔标签（gap label）的双向链表**表示顺序（经典
order-maintenance 思路）：

- 顺序比较是一次整数比较（O(1)），搜索窗口判定因此为 O(1)；
- 重排只需摘下 |δ| 个点、作为一个整块接到窗口边缘，O(|δ| log |δ|)
  （log 来自按当前标签稳定排序）；
- 标签按大步长间隔放置；局部空隙放不下整块时触发一次全表均匀重编号
  （在本项目规模下极少发生）。

搜索使用可复用的时间戳标记数组而非每次新建哈希集合，避免逐次插入的堆分配。
`visited` 字段如实统计本次搜索**实际访问到的不同顶点数**。

### 朴素小规模参考（`src/naive_topo.hpp`）

每次插入都把图复制一份、加入候选边，然后从零跑 Kahn：
- 跑完所有顶点 ⇒ 无环，提交并用 Kahn 结果作为新序；
- 跑不完 ⇒ 有环，拒绝（原图未动），并用 DFS 复原环路径。

刻意写成 O(n+m)/次 的直白实现，作为可审计基线；它**从不参与**服务端求解，
只被差分测试与性能对照工具使用。项目另有独立的 Kahn 与全量校验函数
（`src/topo.hpp` 末尾），供服务端 `validate` 与测试使用。

---

## 2. 目录结构

```
src/json.hpp        最小 JSON 解析/序列化（无第三方依赖）
src/topo.hpp        增量算法 IncrementalTopo + Kahn 参考 + 全量校验
src/naive_topo.hpp  朴素"每次全量重算"参考实现
src/main.cpp        JSON 命令行服务（stdin/stdout，换行分隔 JSON）
tests/test_topo.cpp C++ 差分/场景测试（增量 vs 朴素，33 万+ 断言）
tests/e2e_server.py Python 端到端测试（真实子进程 + 独立 Python Kahn）
tests/bench.cpp     核心算法微基准（可复现种子、规模）
tests/compare.cpp   增量 vs 朴素全量：决策一致性 + 耗时对照
examples/request.jsonl   请求样例
examples/response.jsonl  对应实际响应（由程序生成）
Makefile
```

---

## 3. 构建

需要 g++（支持 C++17）与 GNU make；端到端测试需要 python3。

```bash
make            # 构建 build/topo_srv 与 build/test_topo
make test       # 构建并运行全部自动化测试（C++ + Python 端到端）
make bench      # 核心算法规模基准
make compare    # 增量 vs 朴素全量 对照
make demo       # 用 examples/request.jsonl 跑一遍服务
make clean
```

---

## 4. JSON 接口

协议：**stdin 每行一个 JSON 请求，stdout 每行一个 JSON 响应**（换行分隔 JSON）。
也支持单对象批量：`{"ops":[ ... ]}`，按序执行后在 `results` 中逐一返回。
错误不致命，会话继续；非法 JSON 返回 `{"ok":false,"error":"bad_json",...}`。

顶点为整数 id `0..n-1`；规模上限 `n <= 200000`（`src/main.cpp` 中
`kMaxN`）。

| 请求 | 说明 |
|---|---|
| `{"op":"reset","n":6}` | 建空图，初始序 `0,1,…,n-1` |
| `{"op":"addEdge","u":5,"v":4}` | 插入边 `u -> v` |
| `{"op":"order"}` | 取当前拓扑序 |
| `{"op":"hasEdge","u":5,"v":4}` | 查询边是否存在 |
| `{"op":"stats"}` | 顶点/边计数 |
| `{"op":"validate"}` | 用独立 Kahn 全量复核当前序 |
| `{"op":"quit"}` 或 EOF | 结束会话 |

`addEdge` 响应字段：

```jsonc
{
  "ok": true,
  "edge": "0->5",
  "accepted": false,      // 是否接受
  "duplicate": false,     // 是否为已存在的重复边
  "cycle": true,          // 是否因成环被拒绝
  "reordered": false,     // 是否发生了局部重排
  "visited": 7,           // 本次搜索实际访问的不同顶点数
  "cyclePath": [0,5,4,3,2,1,0], // 成环时的具体闭合路径
  "graphUnchanged": true, // 成环拒绝时，图与序确实未改变（revision 取证）
  "edges": 5
}
```

快速试用：

```bash
echo '{"op":"reset","n":3}' | ./build/topo_srv
# 或
./build/topo_srv < examples/request.jsonl
```

---

## 5. 请求样例（节选）

`examples/request.jsonl`：逆序构造长链 `5->4->…->0`，再尝试闭合边 `0->5`
（应拒绝并返回整条链构成的环），再试重复边与自环：

```jsonl
{"op":"reset","n":6}
{"op":"addEdge","u":5,"v":4}
...
{"op":"order"}
{"op":"addEdge","u":0,"v":5}
{"op":"addEdge","u":5,"v":4}
{"op":"addEdge","u":3,"v":3}
{"op":"validate"}
```

实际输出见 `examples/response.jsonl`，其中闭合边被拒：
`"cyclePath":[0,5,4,3,2,1,0]`，`"graphUnchanged":true`。

---

## 6. 自动化测试

### 6.1 C++ 差分与场景测试 `tests/test_topo.cpp`

用**同一条边流**同时驱动增量实现与朴素全量参考，逐条比对
接受/成环/重复决策与边数，并用独立校验器验证增量序；覆盖：

- **自环**：全部拒绝，见证 `[i,i]`，图与序不变；
- **逆序长链**（n=500）：每条边都反向、都触发重排，最终序为 `n-1,…,0`；
- **孤立点**：序始终是 `0..n-1` 的一个排列，孤立点不丢不重；
- **重复边**：幂等、零访问、零重排、边只计一次；
- **显式环 + 失败原子性**：返回精确环路径，拒绝后边不存在、序不变、
  边数不变，且图仍可继续插入合法边；
- **随机差分 fuzz**：400 个小规模穷尽式种子 + 多个中规模稀疏/稠密流；
- **实际访问节点统计**：输出每次重排平均/最大访问数。

### 6.2 Python 端到端 `tests/e2e_server.py`

以真实子进程启动 `build/topo_srv`，走完整 JSON 协议，并用**独立的 Python
Kahn** 交叉验证每条随机流；额外覆盖协议错误、越界 id、批量 `ops`、以及经
协议的 n=1000 逆序长链与闭环。

---

## 7. 实际运行记录（本机实测，未通过项：无）

环境：Ubuntu 24.04，g++ 13.3.0（`-std=c++17 -O2 -Wall -Wextra -Wpedantic`，
零警告），Python 3.12.3。以下命令均可在仓库根目录复现。

### 7.1 构建与全部测试 — `make test`

```
$ make clean && make test
g++ ... -o build/test_topo
g++ ... -o build/topo_srv
./build/test_topo
[scenario] self loops
[scenario] reverse-order long chain n=500
           total visited nodes: 125249 over 499 reordered inserts (avg 251.00)
[scenario] isolated vertices
[scenario] duplicate edges
[scenario] explicit cycle + failed insert atomicity
[scenario] randomized differential fuzzing
           inserts=58008 accepted=15422 duplicates=14256 cycles=28330
           reordered inserts=3207, total visited=24401, avg=7.61
[scenario] visited-node statistics (n=2000 mixed stream)
           trivial=1109 reordered=1880 visited(total/avg/max)=15401/8.19/482

checks=334379 failures=0
RESULT: PASS
python3 tests/e2e_server.py
[e2e] protocol, self-loop, cycle, duplicates, isolated vertices
[e2e] reverse-order long chain through JSON protocol (n=1000)
           total visited=500499 avg/insert=501.00
[e2e] randomized streams vs independent Python Kahn
           reordered inserts=435 total visited=2022 avg=4.65

checks=33631 failures=0
RESULT: PASS
```

说明：逆序长链是最坏情形之一（每次插入都在窗口里一路搜索到对端），
平均访问 (n+1)/2 ≈ n/2，与输出一致（n=500 → 251；n=1000 → 501）。随机流中
δ 集合通常很小（平均访问个位数），这正是有界双向搜索的预期行为。

### 7.2 增量 vs 朴素全量 — `make compare`

```
$ ./build/compare 300 3000 20260925
n=300 attempts=3000 seed=20260925
accepted=1904 duplicates=32 cyclesRejected=1064
decisionMismatches=0 incrementalOrderValid=yes
timeIncremental=0.0030s timeNaiveFullRecompute=0.1156s speedup=38.5x

$ ./build/compare 100 9000 424242
accepted=3006 duplicates=1642 cyclesRejected=4352
decisionMismatches=0 incrementalOrderValid=yes
timeIncremental=0.0060s timeNaiveFullRecompute=0.1254s speedup=21.0x
```

每个接受/拒绝/重复决策与朴素全量完全一致（`decisionMismatches=0`）。

### 7.3 限定规模性能 — `make bench`

```
$ ./build/bench
n=100000 attempts=200000 seed=7
accepted=198398 duplicates=2 cyclesRejected=1600 edges=198398
reordered=36164 visitedNodes=13631561 avgVisitedPerReorder=376.94
time=4.425s throughput=45202 attempts/s
independentValidation=OK
```

同一 10 万顶点 / 20 万次插边工作流经 **JSON 协议**实测约 5.4s，最后
`{"op":"validate"}` 返回 `referenceAcyclic=true, incrementalOrderValid=true`。

### 7.4 环路径与失败原子性（手工冒烟）

逆序建链后插入 `0->5`：

```jsonc
{"accepted":false,"cycle":true,"cyclePath":[0,5,4,3,2,1,0],
 "graphUnchanged":true,"edges":5, ...}
```

`graphUnchanged` 由内部单调 `revision` 在 O(1) 内核验：拒绝与重复插入都不
递增版本号，随后再查 `hasEdge(u,v)==false`。

---

## 8. 边界、规模与取舍

- 顶点数硬上限 200000；越界 id、非法 `n`、缺字段均返回结构化错误，不崩溃。
- 顺序比较用 64 位间隔标签；标签空间不足时整表重编号（相对标签步长
  2^20，常规使用下不会触发；触发也是一次性 O(n)）。
- 服务端为单进程、串行处理的 stdin/stdout 工具，便于重定向与脚本化；
  未实现网络监听（题目明确不做前端，纯后端即可）。
- 未通过项 / 已知缺陷：**无**；`make test` 全绿。若修改算法，可先用
  `tests/test_topo.cpp` 的 33 万+ 差分断言兜底。
