# 点云地面分割后端（Python + NumPy + FastAPI）

纯后端服务：对 3D 点云做**分块、带种子的 RANSAC 局部平面拟合**，再用法向倾角与
点到平面距离把每个点分成 `ground` / `non_ground` / `undecidable`。

核心设计原则（与需求逐条对应）：

- **不把最大平面自动视为地面**。一个平面必须同时通过
  「最大倾角」和「最小内点比例 + 最小内点绝对数量」三道硬门槛才被承认为地面；
  否则整块返回 `undecidable`，并在 `reason` 字段说明拒判原因。
- **带种子的 RANSAC**：随机种子全程可复现；调用方还可给先验地面控制点
  （`seed_indices`），它们构成 RANSAC 的首个三点假设并在后续迭代中以高概率被采样。
- **分块保持全局坐标与全局点 ID**：块只决定“用哪些点”，坐标不平移，
  平面方程直接定义在全局坐标系。
- **重叠块投票冲突按明确置信规则合并**：同标签取最大置信；冲突时只有
  高置信严格按 `conflict_margin` 倍数胜出才采纳，否则保守判 `undecidable`。
- **计算与密码操作全部真实执行**：SHA-256 请求指纹、可选 HMAC-SHA256 响应签名，
  均由标准库 `hashlib`/`hmac` 真实计算，验收脚本独立复算并验签（含篡改检测）。

## 目录结构

```
src/groundseg/
  params.py       全部参数与物理含义、参数校验
  planes.py       三点定面 / SVD 最小二乘拟合 / 带种子 RANSAC
  tiles.py        XY 网格分块（重叠、核心格、全局下标）
  segment.py      逐块地面判定、点级置信投票、冲突合并
  metrics.py      精确率/召回率（undecidable 的拒判语义）
  scenes.py       8 个人工合成场景（带逐点真值）
  crypto.py       SHA-256 / HMAC-SHA256（真实密码学操作）
  schemas.py      HTTP 协议模型（Pydantic v2）
  app.py          FastAPI 路由
tests/            81 个自动化测试（pytest）
examples/         示例请求 (*.request.json) 与人工真值 (*.truth.json)
scripts/
  generate_examples.py  重新生成示例
  run_metrics.py        库内分割 + 指标表
  acceptance_http.py    真实 HTTP 验收（独立复算哈希/HMAC）
  accept.sh             一键验收（测试 + 起服务 + HTTP 验收）
requirements.txt      直接依赖（版本区间）
requirements.lock     完整冻结清单（验收环境实测）
```

## 本地启动

需要 Python 3.10+（验收环境为 Python 3.12）。

```bash
# 1) 安装依赖（已满足可跳过）
pip install -r requirements.txt          # 或 pip install -r requirements.lock 复现冻结版本

# 2) 启动服务
PYTHONPATH=src uvicorn groundseg.app:app --host 127.0.0.1 --port 8000
```

可选：设置 `GROUNDSEG_HMAC_KEY` 环境变量后，响应会带 HMAC-SHA256 签名：

```bash
GROUNDSEG_HMAC_KEY=my-secret PYTHONPATH=src uvicorn groundseg.app:app --port 8000
```

健康检查：

```bash
curl http://127.0.0.1:8000/healthz
curl http://127.0.0.1:8000/api/v1/info      # 默认参数、标签与拒判原因说明
```

## 一键验收（推荐）

```bash
bash scripts/accept.sh
```

它会依次执行：依赖检查 → 重新生成示例 → 库内精确率/召回率表 → 81 个 pytest
→ 在后台启动**真实 uvicorn 服务** → 用标准库 HTTP 客户端逐场景请求并独立复算
SHA-256/HMAC、断言指标与 422 错误处理。任一步失败立即非零退出。

分步验收命令：

```bash
python3 -m pytest tests/ -q                       # 仅跑自动化测试
PYTHONPATH=src python3 scripts/run_metrics.py     # 仅看指标表

# 手动 HTTP 调用（服务已启动时）
curl -s -X POST http://127.0.0.1:8000/api/v1/segment \
  -H 'content-type: application/json' \
  --data @examples/flat_ground.request.json | python3 -m json.tool | head -40
```

## 算法说明

### 1. 分块（`tiles.py`）

- 以点云 XY 包围盒左下角为原点，按 `tile_size`（默认 4.0，单位与点云相同）
  划分网格；每个点恰好属于一个**核心格**。
- 每个块按 `tile_overlap`（默认 0.5，即半格）向四周外扩吸收邻居点，
  因此边界带的点会被多个块独立投票。
- 块只保存全局点下标；拟合使用原始全局坐标。`tile_size<=0` 退化为单块。

### 2. 带种子的 RANSAC（`planes.py`）

- 平面方程 `n·x + d = 0`，SVD 最小奇异向量定法向，法向统一朝 +Z。
- 每个假设取 3 点定面（共线重试），统计点到平面距离 ≤
  `distance_threshold`（默认 0.15）的内点；取内点最多的假设，再对全部内点做
  一次 SVD 重拟合。
- **种子机制（两层）**：
  1. 随机种子 `seed_rng`：每个块由 `(seed_rng, 网格列, 网格行, 序号)` 派生
     独立 `numpy.random.Generator`，同输入同参数逐点复现；
  2. 先验种子 `seed_indices`：若提供（去重后 ≥3 个），第一个模型假设直接取
     种子前三点；之后每次迭代 50% 概率从种子池采样、50% 全局均匀采样。
- 采样池对数值重复点去重，避免重复扫描造成退化采样；内点统计仍覆盖全部副本。

### 3. 地面判定的硬门槛（`segment.py`）

块拟合出最大平面后，**必须全部满足**才算可信地面，否则整块 `undecidable`：

| 门槛 | 参数 | 默认 | 拒判 reason |
|---|---|---|---|
| 唯一点数（去重后） | `min_unique_points` | 12 | `insufficient_unique_points` |
| 平面法向与 +Z 夹角 | `max_tilt_deg` | 20° | `tilt_exceeds_max` |
| 内点比例 | `min_inlier_ratio` | 0.5 | `inlier_ratio_too_low` |
| 内点绝对数量 | `min_inlier_count` | 20 | `inlier_ratio_too_low` |

通过后逐点按距离二次判定（`point_margin=1.5`）：
`|n·p+d| ≤ 1.5·阈值` 投 `ground`，否则投 `non_ground`，
点级置信度随距离余量和块级余量（倾角余量 × 内点比例余量）连续变化。

### 4. 重叠区冲突合并（明确置信规则）

对每个全局点收集所有块投票：

1. 无任何块投票 → `undecidable / not_covered`；
2. 投票标签一致 → 取该标签，置信度取所有投票的**最大值**；
3. `ground` 与 `non_ground` 冲突 → 设两侧最高置信 `hi ≥ lo`：
   - `hi > conflict_margin · lo`（默认 1.25，**严格大于**，边界也算打平）
     才采纳高置信一侧（`conflict_resolved_ground/non_ground`）；
   - 否则保守判 `undecidable / conflicting_votes`。

### 5. 指标口径（`metrics.py`）

- `precision = TP/(TP+FP)`，`recall = TP/(TP+FN)`，其中 **undecidable 对正类算
  漏判（FN）、对负类不算误报**——主召回率体现“保守拒判”的代价；
- `decided_recall` 只在系统做出明确判定的正类样本上统计，区分“判错”与“拒判”；
- 非地面类给出对称的精确率/召回率；
- `undecided_rate` 为拒判点占比。

## HTTP 协议

### `POST /api/v1/segment`

请求：

```json
{
  "points": [[x, y, z], ...],
  "point_ids": ["可选字符串ID，长度=点数且唯一"],
  "seed_indices": [0, 12, 45],
  "params": { "max_tilt_deg": 20.0, "tile_size": 4.0 }
}
```

响应（节选）：

```json
{
  "request_sha256": "<对原始请求字节的 SHA-256，可用外部工具复算>",
  "request_digest_algorithm": "SHA-256",
  "response_sha256": "<对规范化响应体的 SHA-256>",
  "n_points": 1024,
  "params_used": { "...": "实际生效参数" },
  "stats": {"total": 1024, "ground": 1024, "non_ground": 0,
            "undecidable": 0, "blocks": 9, "decided_blocks": 9},
  "blocks": [
    {"tile": [0, 0], "decided": true, "reason": "ground_plane",
     "inlier_ratio": 0.91, "inlier_count": 118, "tilt_deg": 0.4,
     "plane_normal": [0.0, 0.0, 1.0], "plane_offset": -0.001,
     "confidence": 0.72, "n_points": 320, "n_unique": 320}
  ],
  "points": [
    {"id": "0", "global_index": 0, "label": "ground",
     "confidence": 0.98, "reason": "ground_vote"}
  ],
  "signature": {"algorithm": "HMAC-SHA256",
                "signed_fields": "all_except_signature",
                "mac": "<仅当设置 GROUNDSEG_HMAC_KEY 时出现>"}
}
```

- 规范化签名序列化：`json.dumps(..., sort_keys=True, separators=(",",":"),
  ensure_ascii=False)`；HMAC 覆盖除 `signature` 外的全部字段（含
  `response_sha256`）。
- 非法点维数 / NaN / 越界种子 / 重复 ID / 非法参数一律返回 `422`。

### `POST /api/v1/evaluate`

请求 `{"predicted": ["ground", ...], "truth": [1, 0, ...]}`，
返回两类标签的 precision/recall/decided_recall/undecided_rate 与混淆计数。

### 合成场景与实测指标（默认参数，随机种子固定）

| 场景 | 点数 | 地面精确率 | 地面召回率 | undecidable |
|---|---:|---:|---:|---:|
| `flat_ground` 水平地面 | 1024 | 1.000 | 1.000 | 0% |
| `sloped_ground` 10° 斜坡 + 空中杂点 | 1224 | 1.000 | 1.000 | 0% |
| `steep_slope` 35° 陡坡 | 1224 | — | — | **100%（拒判）** |
| `ground_with_wall` 地面 + 竖直墙 | 1700 | 1.000 | 1.000 | 0% |
| `sparse_ground` 9 个稀疏点 | 9 | — | — | **100%（拒判）** |
| `diffuse_noise` 空间均匀噪声 | 400 | — | — | **100%（拒判）** |
| `ground_with_noise` 噪声 + 离群点 | 1174 | 1.000 | 1.000 | 0% |
| `duplicate_points` 8 倍重复点 | 3200 | 1.000 | 1.000 | 0% |

35° 陡坡在把 `max_tilt_deg` 放宽到 40° 后即可正常分割
（见 `examples/steep_slope.loose.request.json`），说明拒判确实由倾角门槛驱动，
而非程序缺陷。大墙单元测试还验证了：即便纯墙块内点比例高达 100%，竖直平面
照样被倾角门槛拒判——**最大平面从来不会被自动当作地面**。

## 测试

```bash
python3 -m pytest tests/ -q
# 81 passed：平面几何/RANSAC、分块与冲突规则、指标、密码学（含与 sha256sum/openssl 对照）、
#            HTTP 协议（真实 ASGI TestClient）、8 个场景的指标契约
```

## 局限性（如实说明）

- “地面”语义由相对 +Z 的倾角与局部平面内点比例定义；阶梯、多级地形、
  拱形地面不在当前模型内。
- 距离单位完全沿用输入单位，默认阈值（0.15）按“米级车载/机器人点云”取值，
  其他尺度应通过 `params` 调整。
- 分块在 XY 平面进行，对纯竖直立面（如室内多层结构）没有特殊处理；
  这类块按规则被拒判为 `undecidable` 而非乱标。
