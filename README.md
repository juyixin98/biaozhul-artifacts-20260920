# SO(3) 旋转平均与异常值服务

纯后端 HTTP 服务：输入一组带权单位四元数，输出 SO(3) 上的稳健加权平均旋转。
使用测地残差 + Huber 稳健权重（IRLS），统一处理 q / -q 反号等价，
输出收敛状态与对称多解提示。

## 技术栈

- Python 3.12，FastAPI + Uvicorn（HTTP 接口），NumPy（核心计算），SciPy（依赖锁定保留），pytest（测试）
- 全部依赖锁定于 `requirements.txt`（`pip freeze` 生成）

## 算法说明

1. **反号等价统一**：所有输入四元数归一化后，翻转符号使与第一个输入点积非负（同一半球）。
2. **初始化**：Markley 弦式 L2 平均 —— 取累加矩阵 `M = Σ wᵢ qᵢ qᵢᵀ` 的最大特征向量。
3. **IRLS 精化**：在当前均值的切空间内迭代。每次计算误差四元数的对数映射（旋转向量），
   其模长即测地残差；按 Huber 核计算稳健权重（残差 ≤ δ 权重为 1，否则 δ/r），
归一化后
   加权平均得到步长，指数映射更新均值。步长模长小于 `tolerance` 即收敛。
4. **多解提示**：累加矩阵 M 前两大特征值的相对间隙 `(λ₁−λ₂)/λ₁` 小于阈值（默认 1e-3）时，
   说明输入分布接近对称，存在多个同等优的平均解，置 `multi_solution_hint=true` 并附消息。
5. **异常值**：收敛后稳健权重低于 0.5 的输入列入 `outlier_indices`。

## 安装与启动

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 启动服务（默认 8000 端口，如被占用可换端口）
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
```

## 运行测试

```bash
.venv/bin/python -m pytest tests/ -v
```

## HTTP 接口

### `GET /health`

返回 `{"status": "ok"}`。

### `POST /api/v1/average`

请求体：

```json
{
  "quaternions": [[w, x, y, z], ...],
  "weights": [1.0, 2.0, ...],
  "options": {
    "max_iterations": 100,
    "tolerance": 1e-12,
    "huber_delta": 0.5
  }
}
```

- `quaternions`：必填，至少 1 个，四元数顺序为 `[w, x, y, z]`，q 与 -q 等价（无需手工统一符号）；零范数会被拒绝（422）。
- `weights`：可选，正数，长度与四元数一致；缺省为等权。
- `options.huber_delta`：Huber 截断（弧度），默认 0.5；调大趋近普通 L2 平均，调小抗异常值更强。

响应体：

| 字段 | 含义 |
|---|---|
| `converged` | 是否在 `max_iterations` 内收敛 |
| `iterations` | 实际迭代次数 |
| `quaternion` | 平均旋转 `[w, x, y, z]`（单位范数，w ≥ 0） |
| `rotation_matrix` | 对应 3×3 旋转矩阵（正交且行列式为 1） |
| `geodesic_residuals_rad` | 每个输入到均值的测地残差（弧度） |
| `robust_weights` | 每个输入的最终 Huber 稳健权重 |
| `outlier_indices` | 被判为异常值的输入下标 |
| `multi_solution_hint` | 对称多解提示 |
| `eigen_gap` | 累加矩阵相对特征间隙 |
| `messages` | 附加说明（未收敛、多解等） |

### 请求样例

```bash
# 示例文件：4 个接近单位旋转的四元数（含一个反号输入）+ 1 个绕 x 轴 90° 的异常旋转
curl -s -X POST http://127.0.0.1:8000/api/v1/average \
  -H "Content-Type: application/json" \
  -d @examples/average_request.json | python3 -m json.tool
```

实测响应（节选）：

```json
{
  "converged": true,
  "iterations": 12,
  "quaternion": [0.998013768595699, 0.062464862139645756, 1.19e-19, 0.008164477404332836],
  "geodesic_residuals_rad": [0.1261, 0.1264, 0.1351, 0.1359, 1.4459],
  "robust_weights": [1.0, 1.0, 1.0, 1.0, 0.3458],
  "outlier_indices": [4],
  "multi_solution_hint": false,
  "eigen_gap": 0.9038
}
```

异常输入（下标 4，绕 x 轴 90°）被自动降权至 0.346 并列入 `outlier_indices`。

对称多解示例：

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/average \
  -H "Content-Type: application/json" \
  -d '{"quaternions": [[1,0,0,0],[0,1,0,0]]}'
# → "multi_solution_hint": true, "eigen_gap": 0.0,
#   messages 提示 "multiple averages may be equally valid"
```

## 验收覆盖（自动化测试）

`tests/` 共 22 个用例，全部通过：

- **q 与 -q 等价**：半数输入翻号后平均结果不变（测地差 < 1e-10）；单个 q 与 -q 各自平均均还原自身。
- **跨 180° 边界**：+179° 与 −179°（绕 z 轴）平均 ≈ 180° 绕 z 旋转（误差 < 1e-9 rad）；π 附近簇平均不发散。
- **单个异常旋转**：6 个 ±2° 内点 + 1 个 90° 外点，稳健均值距单位旋转 < 5°，外点被标记且权重最低；对照大 `huber_delta`（退化为 L2）验证稳健性确实起作用。
- **旋转矩阵合法性**：`R Rᵀ = I`、`det(R) = 1`（容差 1e-12），输出四元数单位范数。
- **小范围解析近似**：绕固定轴小角度旋转的加权平均角度 = 角度加权均值（误差 < 1e-12）；同轴两旋转等权平均 = 中点角度。
- **多解提示**：单位旋转与绕 x 轴 180° 等权输入触发 `multi_solution_hint`（特征间隙为 0）；紧簇不触发。
- **输入校验**：零范数、权重长度不匹配、负权重均返回 422。

## 实测结果记录

- `pytest tests/ -v`：**22 passed**（Python 3.12.3，pytest 9.1.1，0.53s）。
- 实机启动 `uvicorn app.main:app` 并通过 curl 验证 `/health` 与 `/api/v1/average`
  （含 `examples/average_request.json` 与对称多解样例），响应如上，行为符合预期。
- 本机 8000 端口被其他进程占用，实测使用 8377 端口验证。

## 已知限制 / 未完成项

- Huber 是单调权重而非红降权重，极端异常值仍会对均值有少量拉动（示例中约 7°）；
  如需完全剔除可改用 Tukey 双权重或截断最小二乘，目前未实现。
- 对称多解情形下 IRLS 会收敛到其中一个解（受初始化影响），服务只给出提示，
  不枚举全部等价解。
- 未做请求体大小限制、认证与速率限制；仅监听本地回环地址，生产部署需自行加固。
- SciPy 已锁定但当前实现未直接调用（核心计算仅用 NumPy），保留以便后续扩展
  （如 `scipy.spatial.transform.Rotation` 交叉校验）。
