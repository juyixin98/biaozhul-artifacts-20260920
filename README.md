# sparse-retrieval

稀疏向量余弦相似检索的纯后端服务。Python + NumPy 实现，不下载任何外部模型或数据；
用可复现的合成数据（NumPy 种子随机数）验证核心机制。

## 核心语义（精确定义）

| 主题 | 语义 |
|------|------|
| 重复维度 | 输入为 `[dim, weight]` 对列表；同一维度出现多次时**权重求和合并**；合并后为 `0.0` 的项被丢弃 |
| 负权重 | 一等公民：存储、索引、点积均保留符号，余弦分数可为负 |
| 零向量 | 无任何非零项（空输入或权重相互抵消）的向量范数为 `0.0`；它与任何向量的余弦**定义为 `0.0`**。零向量文档可存储、可按 id 删除，但不进入任何倒排表 |
| TopK 稳定性 | 排序键为 `(-score, doc_id)`：分数降序，**并列分数按 doc_id 升序**，结果完全确定 |
| 剪枝 | 只做**不改变精确结果**的剪枝：倒排索引只扫描与查询共享 ≥1 个维度的候选文档（非候选文档点积恒为 `0.0`，余弦恒为 `0.0`，随后按 `0.0` 补齐参与 TopK，保证与全扫描逐一相同）；另跳过零权重查询维与索引中不存在的维度。**不做**任何近似剪枝 |
| 候选数报告 | 每次查询返回 `candidates_examined`（实际打分的候选文档数）与 `num_docs` |

**位级一致性**：索引与全扫描对每个文档按"查询维度升序"的相同顺序累加点积，
因此两者分数**逐位相等**（不是近似相等），验收测试按此断言。

## 目录结构

```
sparse_retrieval/
  vector.py       # SparseVector：重复维度合并、零项丢弃、范数、输入校验
  index.py        # InvertedIndex：倒排索引、精确 TopK、候选数统计
  brute_force.py  # 全扫描参照实现（与索引相同的累加顺序）
  synthetic.py    # NumPy 种子化合成数据（可复现，无外部下载）
  server.py       # stdlib http.server JSON 服务（无 Web 框架依赖）
tests/            # pytest：向量语义 / 索引行为 / 验收等价性 / HTTP 端到端
scripts/run_acceptance.py   # 验收脚本：索引 vs 全扫描 vs NumPy 稠密参照
examples/requests.sh        # curl 请求样例
```

## 快速开始

```bash
pip install -r requirements.txt          # 仅 numpy 与 pytest

# 运行自动化测试
python3 -m pytest tests/ -q

# 运行验收（随机稀疏数据 vs 全扫描，报告候选数）
python3 scripts/run_acceptance.py

# 启动服务
python3 -m sparse_retrieval.server --port 8080

# 发送请求样例（另开一个终端）
bash examples/requests.sh
```

## HTTP API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | 健康检查 |
| GET | `/stats` | 文档数 / 维度数 |
| POST | `/documents` | `{"doc_id": 1, "vector": [[dim, weight], ...]}` 插入或替换 |
| DELETE | `/documents/<doc_id>` | 删除文档 |
| POST | `/query` | `{"vector": [[dim, weight], ...], "k": 10}` → TopK |

### 请求样例

```bash
# 文档 1 含重复维度（10 出现两次，合并为 1.5）与负权重（20: -0.75）
curl -s -X POST http://127.0.0.1:8080/documents -H 'Content-Type: application/json' \
  -d '{"doc_id": 1, "vector": [[10, 1.0], [10, 0.5], [20, -0.75]]}'
# -> {"doc_id": 1, "nnz": 2, "norm": 1.6770509831248424, "duplicates_merged": 1}

curl -s -X POST http://127.0.0.1:8080/query -H 'Content-Type: application/json' \
  -d '{"vector": [[10, 1.0], [20, 0.5]], "k": 2}'
# -> {"results": [{"doc_id": 2, "score": 0.882...}, {"doc_id": 1, "score": 0.6}],
#     "candidates_examined": 2, "num_docs": 4}
```

完整样例见 `examples/requests.sh`（含零向量查询、删除、非法输入 400）。

## 实际运行记录（如实）

环境：Python 3.12.3，NumPy 2.5.3，pytest 9.1.1，Linux 6.8。

### 自动化测试

```
$ python3 -m pytest tests/ -q
................................................                         [100%]
48 passed in 6.05s
```

覆盖：重复维度合并（含抵消为零）、负权重、零向量（文档与查询）、并列分数按
doc_id 稳定排序、`k > 候选数` 时以 0.0 补齐、文档更新/删除、非法输入 400、
5 组随机种子下索引与全扫描逐位一致、NumPy 稠密参照交叉校验、候选数独立复核。

### 验收脚本

```
$ python3 scripts/run_acceptance.py
corpus: 2000 docs, dim 5000, seed 20260925
indexed 2002 docs / 4999 dims in 0.076s
queries: 51, k=10
avg candidates examined: 103.6 / 2002 docs (5.2%)
avg latency: index 3.78 ms, full scan 26.65 ms
OK: all 51 queries match full scan bitwise and dense reference
```

51 个查询（含 1 个零向量查询；语料含 2 个零向量文档）全部与全扫描**逐位一致**，
并通过 NumPy 稠密参照校验；索引平均只检查 5.2% 的文档。

### 服务冒烟（`examples/requests.sh`，端口 18080）

重复维度合并（`duplicates_merged: 1`）、负权重拉低分数（doc 1 余弦 0.6，
低于无负权重维的 doc 2 的 0.882）、零向量文档（`nnz: 0, norm: 0.0`）、
候选剪枝（`candidates_examined: 2 / num_docs: 4`）、零向量查询全部 0.0 且按
doc_id 排序、删除生效、非法维度返回 400 —— 均按预期。

### 开发中遇到并已修复的问题（未通过项记录）

1. `test_negative_weights_included_in_random_equivalence` 初版失败：文档与查询
   都用全负权重，负×负点积为正，断言"应出现负分数"不成立。**测试构造错误**，
   已改为全负文档 × 全正查询（共享维贡献恒为负）。
2. `test_duplicate_dimensions_in_input_are_merged_before_indexing` 初版失败：
   断言并列分数 `== 1.0`，但 `sqrt(2)*sqrt(2) = 2.0000000000000004`，余弦为
   `0.9999999999999998`。这是正确的浮点行为，**测试期望错误**，已改为断言两
   个相同向量分数逐位相等且 `isclose(1.0)`。
3. 服务首次在端口 8123 启动失败：`OSError: [Errno 98] Address already in use`
   （该端口被机器上另一进程占用，样例请求打到了别人的服务上）。换用 18080 后正常。

当前无未通过的测试或验收项。

## 已知限制

- 单进程内存索引，无持久化；服务重启后数据丢失。
- `doc_id` 限定为非负整数（保证并列排序键的确定性）。
- 无鉴权、无 HTTPS，仅面向本地/内网基础设施用途。
- 稠密维度极高且查询极稠密时，候选集退化为全量（剪枝失效但结果仍精确）。
