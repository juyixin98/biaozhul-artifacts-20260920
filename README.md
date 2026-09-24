# 点云地面分割后端（Point-Cloud Ground Segmentation）

纯后端服务：Python + NumPy + FastAPI，零第三方数值/点云库依赖。
用**带种子的 RANSAC** 拟合局部平面，结合**局部法向**与**到平面距离**区分地面点；
空间分块处理，重叠块的分类冲突按**显式置信规则**合并。
**最大平面不会自动当作地面**：平面必须同时通过倾角、内点比例、内点数、粗糙度门限，
否则该区域返回 `unknown`（不可判定），并给出机器可读的原因。

---

## 1. 目录结构

```
.
├── app/
│   ├── main.py                  # FastAPI 应用：/health、/segment
│   ├── models.py                # 请求/响应协议（Pydantic 校验）
│   └── segmentation/
│       ├── geometry.py          # 平面拟合、法向、局部线性度（SVD）
│       ├── ransac.py            # 带种子 RANSAC + 可靠性门限
│       ├── chunking.py          # XY 分块、逐点投票、置信合并
│       ├── metrics.py           # 精确率/召回率/F1（含弃权记账）
│       └── synth.py             # 8 个带构造真值的合成场景
├── examples/                    # 每个场景的请求样例 + 真值（脚本生成）
├── scripts/
│   ├── generate_examples.py     # 生成 examples/*.json
│   └── evaluate_scenes.py       # 全场景验收（精确率/召回率）
├── tests/                       # 54 个 pytest 自动化测试
├── requirements.txt             # 直接依赖（固定版本）
├── requirements.lock            # pip freeze 全量锁定
└── pytest.ini
```

## 2. 本地启动

需要 Python 3.12（3.10+ 亦可）。

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt        # 或 -r requirements.lock 全量复现

# 生成示例输入（幂等、确定性）
.venv/bin/python scripts/generate_examples.py

# 启动服务
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
# 打开 http://127.0.0.1:8000/docs 可交互查看 OpenAPI 文档
```

## 3. 验收命令

```bash
# (1) 自动化测试：54 个，覆盖平面几何/RANSAC 门限/分块合并/HTTP/指标
.venv/bin/python -m pytest -q

# (2) 全场景评测：对人工构造真值输出精确率与召回率
.venv/bin/python scripts/evaluate_scenes.py
# 期望最后一行：ACCEPTANCE PASSED ...
```

真实 HTTP 调用示例：

```bash
curl -s http://127.0.0.1:8000/health
curl -s -X POST http://127.0.0.1:8000/segment \
  -H 'Content-Type: application/json' \
  --data @examples/mixed_world.json | python3 -m json.tool | head -40
```

当前实测结果（确定性，rng_seed 固定）：

| 场景 | 点数 | 精确率 P | 召回率 R | 说明 |
|---|---:|---:|---:|---|
| flat_with_wall（斜坡0°+垂直墙+杆） | 547 | **1.000** | **1.000** | 墙体绝不被判为地面 |
| slope（12°斜坡+杆） | 357 | **0.997** | **0.997** | 倾角门限内的斜坡=地面 |
| moderate_noise（噪声 20%） | 124 | **1.000** | **1.000** | 悬空噪声=非地面 |
| duplicates（40% 重复点） | 94 | **1.000** | **1.000** | 重复坐标投票一致 |
| mixed_world（10°坡+墙+杆+噪声+重叠块） | 449 | **0.989** | **0.994** | 端到端综合场景 |
| steep_slope（32°陡坡） | 276 | — | — | **全部 unknown**（倾角门限，不自动当地面） |
| sparse（仅 8 点） | 8 | — | — | **全部 unknown**（点数不足） |
| noise_dominated（25 地面点 / 66 噪声） | 91 | — | — | **全部 unknown**（内点比例门限） |

对可判定场景的验收门槛为 P≥0.90、R≥0.90；对不可判定场景要求**零过度自信标签**。

## 4. 算法

### 4.1 带种子的 RANSAC（`ransac.py`）

1. **种子集合**：按全局 z 排序取最低 `seed_band_quantile`（默认 25%）的点；
   每次采样三元组以 `seed_sample_probability`（默认 0.7）概率从种子带抽取，
   否则全局均匀抽取。种子=低程带，即最可能贴地的点。
2. **确定性**：RNG 由 `rng_seed` 控制，同输入同参数结果逐位可复现。
3. **采样防退化**：三元组必须来自三个**不同坐标**（重复点先按精确坐标分组），
   共线三元组跳过。
4. **自适应迭代数**：按当前最优内点比例 w，用教材公式
   `N = log(1-confidence)/log(1-w³)`（默认 confidence=0.99）更新，并夹在
   `[min_iterations, max_iterations]`。
5. **最小二乘重拟合**：在最优内点集上用 SVD 重拟合平面，重复至内点集稳定（默认 2 轮）。
6. **可靠性门限（全部通过才算 reliable）**：
   - `max_tilt_deg`（默认 20°）：法向与竖直方向夹角；
   - `min_inlier_ratio`（默认 0.50，按**不同坐标**统计）；
   - `min_inlier_count`（默认 8）；
   - `max_rms`（默认 0.05 m）：内点相对平面的 RMS 厚度；
   - `min_points`（默认 12）。

   任一不过即返回 `status="undecidable"` 与原因码
   （`tilt_exceeded` / `low_inlier_ratio` / `too_few_inliers` /
   `plane_too_rough` / `too_few_points` / `no_valid_sample`），
   **不返回平面、不返回内点掩膜**。门限顺序决定报告原因。

### 4.2 分块与全局 ID（`chunking.py`）

- XY 平面滑动窗口：`chunk_size`（默认 4 m）、重叠 `overlap`（默认 25%）。
  窗口起点保证并集无缝覆盖整个包围盒；点可落入 1–4 个块。
- 每个块独立做 RANSAC；点在整个管线中保持**全局坐标**与**原始数组下标作为全局 ID**，
  块内只持有全局索引，永不重置编号。
- 点数低于 `chunk.min_points` 的块标记 `skipped`，不产生投票。

### 4.3 逐点投票（距离 + 局部法向 + 线性度）

每个点在所属块内得到一票：`ground` / `non_ground`，强度 `strong` / `weak`。

- 局部 k 邻域（默认 k=8）SVD：给出单位法向（朝上定向），并由奇异值比值识别
  一维（线状）邻域——墙的水平行、竖直杆列的特征，平面地面点不会有线状邻域。
- 到拟合平面距离 d，阈值 `distance_threshold`（默认 0.10 m）：
  - d ≤ 阈值 且 法向与平面法向夹角 ≤ ~25.8°（cos≥0.90）→ ground strong；
  - d ≤ 阈值、夹角在 ~25.8°–41.4° → ground weak；
  - d ≤ 阈值但法向近乎水平（|n_z| ≤ 0.35，墙/杆延伸进斜面平面的情形）
    或夹角大且靠近带宽边缘、或邻域线状 → **non_ground strong**；
  - d > 2×阈值 → non_ground strong；阈值～2×阈值之间 → non_ground weak；
  - 局部描述子无效（稀疏/重复邻域）且点位于内半带宽 → ground strong，
    否则 ground weak（不因缺信息而过度自信）。

### 4.4 重叠块冲突合并（`merge_votes`，显式规则，按顺序）

1. 无投票（覆盖它的块全部 undecidable/skipped）→ **unknown**；
2. 各票标签一致 → 该标签，置信度取最高，支持度取最大；
3. 标签冲突且**置信度层级不同** → `strong` 一方胜（无论数值支持度）；
4. 同层级冲突 → 比较两侧**总支持度**（0.7×内点比例 + 0.3×厚度指数，
   对投票数求和）；支持度严格相等（≤1e-6）→ **unknown**，证据平衡时不猜。

合并依据（投票数、双方支持度、参与块 ID）逐点写入响应，便于审计。

## 5. HTTP 协议

`POST /segment`

请求体：

```json
{
  "points": [{"id": 0, "x": 1.0, "y": 2.0, "z": -0.01}],
  "ransac": { "max_tilt_deg": 20.0, "rng_seed": 20260923 },
  "chunk":  { "chunk_size": 4.0, "overlap": 0.25 }
}
```

- `points` 必填非空；`id` 为调用方分配的非负整数，**必须唯一**，响应原样回显；
  坐标必须是有限数（NaN/Infinity 返回 422，错误体不回显非法数值）。
- `ransac` / `chunk` 均可省略，省略即默认值；所有范围约束见 `/docs`。

响应（节选）：

```json
{
  "point_count": 449,
  "reliable_chunks": 6,
  "undecidable_chunks": 0,
  "results": [
    {"id": 0, "label": "ground", "confidence": "strong",
     "vote_count": 2, "support_ground": 1.93, "support_non_ground": 0.0,
     "chunks": ["c01_00", "c01_01"]}
  ],
  "chunk_reports": [
    {"chunk_id": "c00_00", "status": "reliable", "reason": "reliable",
     "point_count": 121, "inlier_count": 121, "inlier_ratio": 1.0,
     "tilt_deg": 0.04, "rms": 0.011, "iterations_used": 20}
  ],
  "request_sha256": "..."
}
```

`label ∈ {ground, non_ground, unknown}`，`confidence ∈ {strong, weak}`
（unknown 固定为 weak）。`request_sha256` 是对规范化点列表真实计算的
SHA-256，用于请求去重/审计。

`GET /health` → `{"status":"ok",...}`；`GET /openapi.json` 为完整模式。

## 6. 测试覆盖（tests/，54 个）

- `test_geometry.py`：三点定面、法向定向、倾角、SVD 共线拒绝、噪声面恢复、
  局部法向（地面 vs 墙）、重复点邻域。
- `test_ransac.py`：平地可靠、**种子可复现**、斜坡 12° 接受 / 30° 拒绝、
  点数门限、内点比例门限、**最大平面（纯墙）不被自动当地面**、
  重复点不破坏采样且标签一致、全重合点不可判定、粗糙度门限、低程种子命中。
- `test_chunking.py`：窗口无缝覆盖、重叠真实存在、全局 ID 不被局部化、
  四条合并规则（一致/强压弱/支持度决胜/平局弃权）、远偏移全局坐标、小块 skip。
- `test_pipeline_scenes.py`：8 个构造真值场景的精确率/召回率门槛与全弃权断言。
- `test_api.py`：健康检查、形状/ID/校验和、样例文件指标、稀疏与陡坡弃权、
  NaN/重复 id/空请求/越界参数 422、抬高倾角门限后 32° 坡可判、OpenAPI 存在。
- `test_metrics.py`：混淆矩阵、unknown 预测按弃权记账、unknown 真值排除。

## 7. 设计取舍与边界

- 数值计算全部为真实 NumPy/SVD 执行；SHA-256 为真实哈希；无桩实现。
- 局部法向用 O(块点数×k) 的穷举距离（每块 8000 点封顶）；
  示例规模（数百点）全场景测试秒级完成。超大云请先按区块切分调用。
- “不可判定”是一等输出：点不足、噪声主导、坡度过陡时明确弃权，
  优于把最显眼的平面误报为地面。
