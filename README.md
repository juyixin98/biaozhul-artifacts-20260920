# 稀疏向量相似检索服务

纯后端项目：用 **Python 3.12 + NumPy** 实现稀疏向量的**余弦相似度 TopK 检索**，
核心是带 **WAND 上界剪枝的倒排索引**。不下载任何外部模型或数据集，
全部使用可复现的合成数据验证。剪枝只减少计算量，**检索结果与全扫描逐位一致**。

## 语义约定（验收要点）

| 主题 | 约定 |
|------|------|
| **重复维度先合并** | 入库/查询前，同一维度的多个权重**求和**；合并后恰好为 0（含异号抵消）的维度删除。见 `merge_entries`。 |
| **零向量语义明确** | 零向量是合法向量；余弦相似度在任一侧为零向量时**统一定义为 `0.0`**（不是 NaN，也不是 1）。零向量查询返回 doc_id 最小的 k 个。 |
| **TopK 稳定** | 排序键固定为 `(分数降序, doc_id 升序)`；并列分数时 doc_id 小者优先，与插入/遍历顺序无关。恰好返回 `min(k, 命中文档数)` 条。 |
| **剪枝不改变结果** | WAND 仅跳过“上界之和都低于当前第 k 名阈值”的文档；维度上界经 `nextafter` 单向抬高 1 ulp 并在 longdouble 中累加，保守安全。负权重取绝对值上界，不会误剪。 |
| **负权重** | 全程支持（点积、余弦、上界、倒排链）。 |

## 目录结构

```
sparse_retrieval/
  vector.py     # SparseVector、重复维度合并、零向量语义、请求解析
  scoring.py    # 稀疏点积、余弦、全扫描 TopK（正确性基准）
  index.py      # 倒排索引 + DAAT-WAND 剪枝 + 候选数统计
  data.py       # 可复现合成数据（主题模型 + 噪声 + 负权重 + 重复维度 + 零向量）
  server.py     # 标准库 http.server 实现的 REST 服务（无第三方 Web 框架）
scripts/
  benchmark.py  # 随机数据：索引 vs 全扫描，报告候选数/剪枝率/耗时/边界场景
tests/          # pytest 自动化测试（unit + integration）
examples/       # curl 请求样例与 JSON 载荷
```

## 环境与安装

仅需 Python 3.10+ 与 NumPy；测试需要 pytest、pytest-cov。

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## 运行服务

```bash
python3 -m sparse_retrieval.server --host 127.0.0.1 --port 8000 \
    --seed 42 --n-docs 2000 --dim 1000
```

启动即用合成数据建好索引。

### HTTP 接口

| 方法 路径 | 说明 |
|-----------|------|
| `GET /health` | 健康检查、维度与文档数 |
| `GET /stats` | 倒排链数量与链长统计 |
| `POST /search` | 余弦 TopK，返回命中与候选数/剪枝统计 |
| `POST /index/rebuild` | 用显式文档或合成数据重建索引 |
| `GET /` | 接口说明 |

向量支持两种 JSON 形态：

```json
{"dim": 1000, "entries": [{"index": 3, "value": -1.5}, {"index": 3, "value": 0.5}]}
```

```json
{"dim": 1000, "indices": [3, 7], "values": [-1.5, 2.0]}
```

空 `entries` / `indices` 表示零向量。检索示例：

```bash
curl -sS http://127.0.0.1:8000/search \
  -H 'Content-Type: application/json' \
  -d '{"k":5,"query":{"dim":1000,"entries":[{"index":12,"value":1.5},{"index":47,"value":-0.8}]}}'
```

返回体里 `candidates` 字段如实报告**实际候选数**：

```json
"candidates": {
  "n_docs": 2000,
  "candidates_scored": 164,      // 实际计算了余弦的文档数（全扫描为 2000）
  "wand_pivot_skips": 8,         // WAND 枢轴安全跳过次数
  "zero_filled": 0,              // 0 分补位文档数（负分/0 分边界）
  "candidates_considered": 164,
  "full_scan_docs": 2000,
  "pruning_ratio": 0.918
}
```

完整请求样例（含重建、零向量、错误样例）见 `examples/requests.sh`，
载荷文件见 `examples/*.json`。

## 验收基准：索引 vs 全扫描

```bash
python3 scripts/benchmark.py                      # 主题相关查询（默认 5000 文档）
python3 scripts/benchmark.py --query-kind rare    # 冷僻短查询，命中率低
python3 scripts/benchmark.py --n-docs 20000 --dim 2000 --n-queries 50
```

脚本对每条查询同时跑全扫描与索引，逐条核对 doc_id 与分数**完全相等**，
并在末尾运行一个手工边界场景（正分命中少于 k、负分、无交集 0 分、零向量、
重复维度抵消）。真实运行结果见 **[RUN_REPORT.md](RUN_REPORT.md)**。

## 测试

```bash
python3 -m pytest                                   # 全部测试
python3 -m pytest -m unit                           # 仅单元测试
python3 -m pytest -m integration                    # 含真实 HTTP 端口的端到端测试
python3 -m pytest --cov=sparse_retrieval            # 覆盖率
```

## WAND 剪枝为什么不改变精确结果

- 每个维度 t 维护上界 `U_t = max_d |w(t,d)| / ‖d‖`，
  查询项贡献上界 `a_t = |q_t|/‖q‖ · U_t ≥ 0`；
- 任意文档真实分数 ≤ 命中项的 `a_t` 之和（取绝对值，故负权重安全）；
- DAAT-WAND 按游标归并，只有当“枢轴及之前各项上界之和 < 当前第 k 名阈值 θ”
  时才整体前移游标——被越过的每个文档都可证明进不了 TopK；
- 当 θ ≤ 0（TopK 尚未被正分填满）时，由于 `a_t ≥ 0`，首个游标即满足
  `cum ≥ θ`，算法退化为多路归并，**所有命中文档都被精确打分**；
  无公共维度的文档分数必为 0，再按 doc_id 升序归并补位。
- 浮点安全：`U_t` 用 `np.nextafter(..., +inf)` 单向抬高 1 ulp，
  WAND 累加与比较在 `longdouble` 中进行，误差只会少剪枝、不会多剪枝。

## 非目标（明确不做）

- 不做前端 / UI；
- 不做持久化、分布式、鉴权（本地基础设施验证项目）；
- 不引入任何外部模型、数据集或除 NumPy 外的重型依赖。
