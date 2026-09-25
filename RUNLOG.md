# RUNLOG —— 实际运行记录

- 日期：2026-09-24
- 环境：Linux 6.8.0-90-generic，Python 3.12.3，NumPy 2.5.3
- 依赖：仅 `numpy`（`requirements.txt` 写为 `numpy>=1.24`，未联网安装，使用环境自带版本）
- 说明：以下命令均在仓库根目录实际执行；输出为真实输出（片段）。
  开发过程中发现 3 个真实缺陷，均已修复并用测试/随机对照回归验证，
  过程如实记录在第 3 节。

## 1. 自动化测试

命令：

```bash
python3 -m compileall -q mospp tests    # 语法编译检查
python3 -m unittest discover -s tests -t .
```

最终结果：

```
Ran 62 tests in 1.5s

OK
```

62 个用例全部通过，分布在：

| 测试文件 | 内容 |
|---|---|
| `tests/test_algorithm.py` | 容差/支配语义、Pareto 过滤、手工图、相等标签、零权环、路径重建、随机枚举对照（120 组 ×2 模式 + 60 组预算） |
| `tests/test_api.py` | 成功响应、全部失败状态、预算、相等标签、两种截断 |
| `tests/test_cli.py` | CLI 文件/stdin/紧凑输出/退出码 0,1,2（子进程端到端） |

## 2. 独立正确性验证（除 unittest 外的随机压力测试）

### 2.1 标签算法 vs 全部简单路径枚举

小图 DFS 枚举所有简单路径 → Pareto 过滤，与标签修正算法对照。
最终一次干净运行（随机种子 424299，300 张图，2–11 节点，
含零权环、自环，simple/walk 两模式）：

```
graphs=300 skipped=0 front_mismatch=0 equal_path_miss=0
```

- `front_mismatch=0`：Pareto **目标点集**两种模式与枚举全部一致；
- `equal_path_miss=0`：simple 模式下枚举得到的每一条 Pareto 最优
  简单路径（含等权路径）都能在主路径或 `equal_paths` 中找到。

开发期间另外跑过多轮（120/200/250/400 组，密度 0.15–0.6，
4–12 节点）以及边界预算穷举（约 1079/298 个“预算恰好等于某条真实
路径权重”的组合 ×2 模式），最终均为 0 失败。

### 2.2 walk 模式较大图性能

```
n=200, m=1500 随机图：front=9, permanent=1112, 耗时 0.06s, truncated=False
```

### 2.3 Pareto 过滤器等价性

`tolerance.pareto_mask` 的 O(N log N) 顺序扫描与按库标量
`dominates` 语义写的 O(N²) 参考实现在 1000 组随机点集
（整数重值/不同尺度浮点、`atol/rtol ∈ {0, 1e-9, 1e-6}`）上：

```
fails 0 of 1000
```

## 3. 开发过程中发现并修复的真实缺陷（如实记录）

### 缺陷 1：起点标签被自己判为重复（首次冒烟测试即暴露）

现象：菱形图上返回 `no_path`，统计 `labels_rejected=1`。

原因：起点标签在搜索开始前就放入了 `permanent[source]`，
弹出时又拿永久标签集合做支配检查，导致它被自己判为“重复”而丢弃，
后续扩展从未发生。

修复：起点标签也进入堆，统一走“弹出 → 支配检查 → 转永久”流程，
不再特殊预置。修复后冒烟测试得到正确前沿 `[(4,5),(6,3)]`。

### 缺陷 2：Pareto 掩码的支配方向写反（随机对照暴露）

现象：首轮 300 组随机图对照立即大面积不一致，例如两条平行边
(0,5) 与 (0,0) 时只留下 (0,5)，真正的最优点 (0,0) 反被过滤。

原因：向量化 `pareto_mask` 中把 `p_i - p_j` 的差值与容差的
比较方向写反了。

修复：改正不等式后，最初仍保留了 O(N²) 向量化版本；之后为避免
枚举器在 6 万条路径上申请 ~56 GiB 临时矩阵，改写为 O(N log N)
的排序顺序扫描（早期时间组提供严格更小的 time，只需维护
最小 cost）。

### 缺陷 3：中间节点状态剪枝误删等权最优简单路径（压力测试暴露）

现象：修复 1、2 后，Pareto **点集**在数百组随机图上都正确，
但有 2/250 张图里枚举找到的个别“与已有点等权、但拓扑不同”的
简单路径没有出现在 `equal_paths`。

根因分析（关键设计点）：最初的同节点状态判据允许
“目标相等 + visited 为严格子集”时剪枝。这保证 Pareto
**目标点集**不丢解，但会丢掉部分实现同一个 Pareto 点的等权简单
路径——而验收要求小图枚举逐条对照、覆盖相等标签。

修复：改为更保守、对“路径覆盖”安全的规则：

- 仅当目标**严格支配**且 `visited(A) ⊆ visited(B)` 才剪枝
  （此时 B 的任何简单延伸都是 A 的合法延伸且更优，安全性有
  “下一节点集合包含关系”证明）；
- 目标相等时仅在 visited 完全相同（重复状态）才剪枝；
- 目标相等 + visited 不同（子集或不可比）一律保留。

修复后所有压力轮次 `equal_path_miss=0`，且 simple 模式仍受
64 节点 / 400 边的规模限制控制状态数。

另：验证过滤器时我写的临时 O(N²) reference 脚本两次把支配方向
写反，一度造成“过滤器大面积不一致”的假象；以库内标量
`tolerance.dominates` 为唯一基准后定位出问题在 reference 脚本，
最终过滤器与基准语义 1000 组随机点全部一致。这是验证脚本错误，
不是库缺陷，一并记录以免误解。

## 4. 样例实际运行

```bash
for f in request_basic request_zero_cycle request_budget \
         request_walk_mode request_truncated; do
  python3 -m mospp examples/$f.json > examples/output/$f.response.json
done
python3 -m mospp examples/request_invalid_negative.json   # 退出码 2
echo '{bad' | python3 -m mospp                            # 退出码 2
```

结果摘要：

| 样例 | status | truncated | Pareto 前沿 (time,cost) |
|---|---|---|---|
| basic | ok | false | (4,5), (6,3) |
| zero_cycle（含零权双向环、零权自环） | ok | false | (3,11), (4,7), (6,3) |
| budget（time≤8,cost≤10） | ok | false | (7,9) |
| walk_mode | ok | false | (11,18), (14,16), (17,15) |
| truncated（node_label_cap=3） | truncated | **true** | 2 个点（可能不完整），11 条节点级截断记录 |
| invalid_negative | 错误响应退出码 2 | — | `error=negative_weight`, field 指向具体边 |

零权环样例另用枚举器独立核对，枚举得到的 Pareto 点与路径
（`s-2-t / s-1-2-t / s-3-t`）与接口响应完全一致。

截断样例的 `truncation` 形如：

```json
[{"node": "l1_0", "reason": "node_label_cap", "dropped_labels": 2}, ...]
```

## 5. 未通过项 / 已知限制

- **无未通过的测试**：62/62 通过，随机对照 0 失败。
- 仅支持非负权重；负权、NaN、Infinity 均按请求错误拒绝（设计如此，
  不是缺陷）。
- `simple` 模式因维护 visited 集合，规模硬限制 64 节点 / 400 边；
  更大的图需显式使用 `mode="walk"`，否则返回 `limit_exceeded`。
- Pareto 前沿可能指数级大；触顶时按设计返回部分结果并报告
  `truncated`，调用方必须检查该标志。
- 未实现 HTTP 服务与前端（需求明确要求纯后端、不做前端）。
