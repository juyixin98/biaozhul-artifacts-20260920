# Trajectory Error Evaluation API (轨迹误差评估)

纯后端离线轨迹评估服务：输入**估计轨迹**与**真值轨迹**，按限定时间差进行关联，
对匹配上的位姿做**刚体对齐**（可选带**尺度**的相似变换，尺度结果单独标记，
绝不与刚体指标混淆），然后计算：

- **ATE**（Absolute Trajectory Error）：逐对位姿的平移欧氏误差 + 旋转**测地线角距**，
  给出 RMSE / mean / median；
- **指定时间跨度 RPE**（Relative Pose Error）：按匹配后序列的索引跨度
  `delta_index ± tolerance_index`（配合实际时间戳即“指定时间跨度”）取相对运动，
  平移误差与旋转角误差分别统计 RMSE。

旋转误差使用 SO(3) 相对旋转的角距 `angle(R_estᵀ R_gt) ∈ [0, π]`，
四元数先做 `qw ≥ 0` 双覆盖规范化，因此四元数在 ±1 附近跨符号边界不会产生
~360° 的假误差。

**明确失败、绝不补零**：重复时间戳、零匹配、退化（共线）对齐、非法位姿等
一律返回结构化 `422` 错误；未匹配位姿直接剔除，误差序列只包含真实匹配对，
不用零填充来压低 RMSE。

技术栈：Python 3.10+、NumPy、FastAPI（仅后端，无前端页面）。

---

## 1. 安装

```bash
cd P059/b
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[test]"          # 或 pip install -r requirements.lock
```

`requirements.txt` 为版本下限约束，**`requirements.lock` 为本次验证锁定的确切版本**
（`pip install -r requirements.lock` 可复现）。

## 2. 本地启动

```bash
source .venv/bin/activate
uvicorn app.main:app --host 127.0.0.1 --port 8000
# 交互式 API 文档： http://127.0.0.1:8000/docs
# OpenAPI JSON：   http://127.0.0.1:8000/openapi.json
# 健康检查：       http://127.0.0.1:8000/healthz
```

可选：设置 `TRAJECTORY_EVAL_HMAC_KEY` 环境变量后，响应会额外携带
`hmac_sha256`（对规范化结果 JSON 的密钥签名）。

## 3. 请求协议 `POST /api/v1/evaluate`

位姿采用 TUM 约定：`position = [x,y,z]`（米），
`quaternion_xyzw = [qx,qy,qz,qw]`（Hamilton 四元数，单位四元数，服务端会归一化）。

```json
{
  "estimated":   [{"time": 0.0, "position": [0,0,0], "quaternion_xyzw": [0,0,0,1]}],
  "ground_truth":[{"time": 0.0, "position": [0,0,0], "quaternion_xyzw": [0,0,0,1]}],
  "association": {"max_time_diff": 0.02},
  "alignment":   {"mode": "rigid"},
  "rpe":         {"delta_index": 1, "tolerance_index": 0}
}
```

| 字段 | 说明 |
|---|---|
| `association.max_time_diff` | 允许的最大 `|t_est − t_gt|`（秒），默认 0.02；最近邻匹配，等距取更早真值；一个真值最多配对一个估计 |
| `alignment.mode` | `rigid`（SE(3)，尺度恒为 1，默认）或 `similarity`（Sim(3)，估计均匀尺度） |
| `rpe.delta_index` | RPE 两端在匹配序列中的索引跨度（≥1），结合匹配对的真实时间戳即“时间跨度” |
| `rpe.tolerance_index` | 容忍的跨度偏差（≥0），默认 0；实际跨度最接近 delta 的配对胜出 |

响应包含：

- `scale_alignment_applied`（布尔，**尺度对齐是否启用的唯一权威标记**）；
- `match`：`n_matched`、对估计/真值/整体的三种 `coverage*`、每对匹配时间差与逐对误差；
- `alignment`：`rotation_matrix`、`translation`、`scale`（刚体模式恒为 `1.0`）；
- `ate`：RMSE/mean/median 与**完整误差序列**；
- `rpe`：配对数、RMSE/mean、每对的起止时间戳与真实 `time_span_s`、误差序列；
- `integrity`：`request_sha256`（原始请求体 SHA-256）、
  `response_sha256`（去掉 integrity 后结果 JSON 的规范化 SHA-256），配置密钥时再加 `hmac_sha256`。

## 4. 验收命令

```bash
source .venv/bin/activate

# 4.1 自动化测试（32 个：几何/匹配/对齐/指标/HTTP/错误路径）
python -m pytest -q

# 4.2 启动服务（另开一个终端，或后台运行）
uvicorn app.main:app --host 127.0.0.1 --port 8000

# 4.3 生成示例输入（examples/*.json 已随仓库提供，可重新生成）
PYTHONPATH=. python examples/generate_examples.py

# 4.4 四种关键场景
for f in pure_translation scale_drift scale_drift_similarity \
         missing_measurements rotation_crossover; do
  echo "== $f =="
  curl -s http://127.0.0.1:8000/api/v1/evaluate \
    -H 'content-type: application/json' --data @examples/$f.json \
    | python scripts/verify_response.py
done

# 4.5 必须明确报错的场景（期望 HTTP 422 + error.code）
for f in err_duplicate_timestamp err_no_matches err_degenerate_alignment; do
  echo "== $f =="
  curl -s -w ' [HTTP %{http_code}]\n' http://127.0.0.1:8000/api/v1/evaluate \
    -H 'content-type: application/json' --data @examples/$f.json
done

# 4.6 HMAC（用密钥重启后）
TRAJECTORY_EVAL_HMAC_KEY=secret uvicorn app.main:app --port 8001
curl -s http://127.0.0.1:8001/api/v1/evaluate -H 'content-type: application/json' \
  --data @examples/pure_translation.json \
  | TRAJECTORY_EVAL_HMAC_KEY=secret python scripts/verify_response.py
```

### 示例预期结果

| 场景 | 预期 |
|---|---|
| `pure_translation` | 刚体偏移（旋转+平移，0.004 s 时间抖动），ATE/RPE 平移与旋转 RMSE ≈ 0，coverage=1.0 |
| `scale_drift`（1.3×，rigid） | 不启用尺度：`scale_alignment_applied=false`，平移 ATE RMSE 明显非零（≈0.53 m），旋转仍为 0 |
| `scale_drift_similarity` | 启用尺度：标志为 true，拟合 `scale≈1/1.3=0.7692`，ATE/RPE 平移 RMSE 回落到 ≈0 |
| `missing_measurements` | 16 个真值只匹配 10 个，coverage=0.625，误差序列长度=10（无零填充），RPE `time_span_s≈0.2 s` |
| `rotation_crossover` | 全局 172° 旋转、四元数交替取反号；测地线角距下 ATE/RPE 旋转 RMSE ≈ 0 |
| 重复时间戳 / 零匹配 / 共线退化 | HTTP 422，`error.code` 分别为 `DUPLICATE_TIMESTAMP` / `NO_MATCHES` / `ALIGNMENT_DEGENERATE` |

## 5. 错误码

| HTTP | code | 触发条件 |
|---|---|---|
| 400 | `INVALID_JSON` | 请求体不是合法 UTF-8 JSON |
| 422 | `INVALID_REQUEST` | 不符合 Schema（字段缺失/取值非法/多余字段） |
| 422 | `INVALID_POSE` / `INVALID_TIMESTAMP` | 非有限数、维度错误、零范数四元数 |
| 422 | `DUPLICATE_TIMESTAMP` | 任一轨迹内存在重复时间戳 |
| 422 | `NO_MATCHES` | 时间窗内没有任何匹配，无法计算指标 |
| 422 | `ALIGNMENT_DEGENERATE` | 匹配点 <2、共线/零延展，或尺度估计无效 |
| 422 | `RPE_NO_VALID_PAIRS` | 匹配序列在该跨度下构造不出任何相对运动对 |

## 6. 计算约定（便于核对）

- 对齐：Umeyama (1991) 闭式解，最小化 `Σ‖gt_i − (sR·est_i + t)‖²`；
  反射保护（det(R)=1）。平面（秩2）点集可解，共线（秩≤1）判为退化并报错。
- ATE 平移：`‖gt_i − (sR est_i + t)‖`；ATE 旋转：`angle((R R_est,i)ᵀ R_gt,i)`。
- RPE 相对运动：`T_{i→j} = T_i⁻¹ T_j`，平移误差 `‖t_rel_gt − t_rel_est‖`，
  旋转误差 `angle(R_rel_estᵀ R_rel_gt)`（弧度转度）。
- RMSE 分母始终是**真实匹配/配对数量**，从不补零。
- 密码学操作真实执行：SHA-256（`hashlib`）与可选 HMAC-SHA256（`hmac`），
  规范化序列化 `json.dumps(sort_keys=True, separators=(",",":"))`，
  可用 `scripts/verify_response.py` 独立复算。

## 7. 项目结构

```
app/
  main.py        FastAPI 路由、错误映射、完整性签名
  schemas.py     Pydantic 请求模型（extra=forbid）
  matching.py    时间窗最近邻关联、重复时间戳检查
  alignment.py   Umeyama SE(3)/Sim(3) 对齐与退化检测
  geometry.py    四元数/SO(3)、测地线角距、SE(3) 相对位姿
  metrics.py     ATE / RPE
  integrity.py   SHA-256 / HMAC / 规范化 JSON
  errors.py      领域错误
examples/        示例输入（成功与必须报错的场景）+ 生成脚本
scripts/verify_response.py  响应哈希/HMAC 独立校验
tests/           32 个自动化测试
requirements.lock            锁定依赖
```
