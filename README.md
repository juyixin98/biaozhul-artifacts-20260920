# polyroots — 复系数多项式全部根的数值求解后端

给定复系数多项式，求全部复根，并给出**可审计的误差证据**（每根残差、因子重构误差、Vieta 偏差、近重根聚类），而不是只回一串浮点数。

## 支持范围

- 任意次数（默认上限 200，可通过 `max_degree` 调整，硬上限见 `app/domain.py`）的复系数多项式求根。
- 系数约定：**降幂**，`coefficients[0]` 为首项系数，`p(z) = c[0]·z^n + … + c[n]`；每个系数为 `[re, im]` 对。
- 算法：伴随矩阵特征值（NumPy/SciPy，平衡化 QR）+ Aberth-Ehrlich 同时迭代精化（`companion+aberth`，默认）；也可选 `companion`（仅特征值）或 `aberth`（从 Cauchy 界圆周初值纯迭代，用于演示迭代耗尽）。
- 高精度参考由 mpmath（50 位十进制）独立生成，见 `scripts/generate_reference.py` 与 `tests/fixtures/reference_roots.json`。**参考答案不由被测内核生成**；对 mpmath 也无法收敛的病态情形（Wilkinson 型、重根），参考取解析已知根并在夹具中标注 `reference_source`。

## 关键取舍（防"正常输入看起来对、边界输入悄悄错"）

1. **每根残差 + 整体因子重构误差都返回**。但对近重根，小残差不能证明准确：重数 m 的根，前向误差可达残差的 1/m 次方。证据层检测根聚类（`cluster_tol` 显式给出），聚类根的 `error_bound` 报告**聚类直径**而非残差派生界，`accuracy_note` 明确拒绝基于残差的精度声明。
2. **零首项规范化，零多项式拒绝**。前导系数按 `1e-14 × 输入尺度` 容差剥离并记录 `leading_dropped`；全部为零（或全部低于容差）返回 `input_invalid`，因为"零多项式的所有根"无定义。常数多项式（0 次）同样拒绝。
3. **根排序稳定，共轭配对容差明确**。输出按 `(re, im)` 全精度全序排列，与内核产生顺序无关；共轭配对是独立步骤，容差 `pair_tol` 在响应 `pairing.pair_tol` 中回显，找不到伙伴的根列入 `unpaired`，绝不强行配对。
4. **迭代耗尽保留未收敛状态**。Aberth 预算耗尽不是异常：响应 `status: "partial"`，未收敛根带着 `converged: false` 和最后迭代值返回，不丢弃、不冒充收敛。

## 错误分类（可区分、可断言）

| category | HTTP | 含义 | 示例 |
|---|---|---|---|
| `input_invalid` | 422 | 数值输入被拒绝 | 零多项式、NaN/Inf 系数、0 次多项式、畸形系数对 |
| `state_conflict` | 409 | 与已记录状态冲突 | 同一 `run_id` 提交了不同载荷 |
| `resource_exhausted` | 413 | 超出资源上限 | 次数超 `max_degree`、`max_iter` 超硬上限 |
| `computation_failed` | 500 | 内核计算失败 | 特征分解失败、迭代出现非有限值 |

同一 `run_id` + 相同载荷 → 幂等重放已存报告；结果也可经 `GET /v1/runs/{run_id}` 取回。

## 模块边界

```
app/validation.py   数值输入边界：解析、规范化、拒绝（不计算）
app/kernel/         计算内核：companion.py / aberth.py（不决定 HTTP 语义）
app/evidence.py     误差证据：残差、重构误差、Vieta（Newton 恒等式独立路径）、聚类
app/ordering.py     稳定排序 + 显式容差共轭配对
app/service.py      编排、run 注册表（重放/冲突）、状态决策
app/runlog.py       JSONL 运行日志（run_id、中间状态、判断理由）
app/api.py          FastAPI 薄层：线格式解析 + 错误分类 → HTTP 映射
app/errors.py       四类错误分类，全层共享
app/domain.py       层间数据契约（系数约定、选项、报告）
```

## 本地启动

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-lock.txt   # 锁定版本；或 requirements.txt 取宽松版本
.venv/bin/python -m uvicorn app.api:app --port 8000
```

运行日志写入 `logs/runs.jsonl`（可用环境变量 `POLYROOOTS_LOG_DIR` 改目录），每个事件带 `run_id`、关键中间状态（规范化结果、迭代扫描数、收敛计数、证据数值）与状态判断理由，可按 `run_id` 重放定位问题。

## 示例请求

```bash
# 2z^4 - 3z^3 + 7z^2 + 7z - 5，根为 1±2i, -1, 0.5
curl -X POST http://127.0.0.1:8000/v1/solve \
  -H 'content-type: application/json' \
  -d '{"coefficients": [[2,0],[-3,0],[7,0],[7,0],[-5,0]], "run_id": "demo-1"}'

# 迭代耗尽演示：纯 Aberth、零预算 → status=partial，根保留且 converged=false
curl -X POST http://127.0.0.1:8000/v1/solve \
  -H 'content-type: application/json' \
  -d '{"coefficients": [[1,0],[-6,0],[11,0],[-6,0]],
       "options": {"method": "aberth", "max_iter": 0}}'

# 零多项式 → 422 input_invalid
curl -X POST http://127.0.0.1:8000/v1/solve \
  -H 'content-type: application/json' \
  -d '{"coefficients": [[0,0],[0,0]]}'
```

响应要点：`roots[]`（每根残差/收敛标志/迭代数/误差界）、`evidence`（最大相对残差、重构误差、Vieta 偏差、聚类、精度说明）、`pairing`（显式容差的共轭配对）、`status`（`converged` / `partial`）。

## 测试

```bash
.venv/bin/python -m pytest            # 39 项
.venv/bin/python scripts/generate_reference.py   # 重新生成高精度参考夹具（可选）
```

测试断言具体数值结果与失败类别（不只"接口能调"）：已知实/复根对照 mpmath 参考、近重根 `(z-1)^4(z-2)` 的聚类与"残差不可信"行为、20 阶稀疏多项式 `z^20−1`、Wilkinson 型 8 次多项式的 Vieta 关系（Newton 恒等式独立路径）、排序置换不变性、配对容差显式性、四类错误各自的 HTTP 状态码、迭代耗尽状态保留、运行日志内容。最近一次运行记录见 `test-results/`。

## 已知限制

- 计算在内核中为 float64；病态多项式（如 Wilkinson 型）前向误差天然受限，测试对此使用与精度匹配的容差并以 Vieta/重构误差为主要证据。需要任意精度时应换内核（当前未提供）。
- 运行注册表为进程内存，重启即失；`GET /v1/runs/{id}` 只服务当前进程生命周期。
- 聚类检测是 O(n²)，次数上限 200 下开销可忽略。
