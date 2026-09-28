# 对称矩阵特征分解服务

对**实对称矩阵**做完整特征分解（特征值 + 特征向量）的多模块 Python 后端。
核心算法为 **Householder 三对角化 + 隐式 Wilkinson 移位 QL 迭代**（EISPACK
`tql2` 型），对任意真实输入实际计算——没有硬编码演示路径。结果用残差、
正交性、重构三类后验证据把关，迭代预算耗尽时**明确报失败**，不会因为
"迭代停了"就返回成功。

## 目录结构

```
eigenservice/
  config.py       规模 / 相对容差 / 迭代预算等全部可配置项 (EIGEN_* 环境变量)
  errors.py       稳定的失败类别 (error_code) 与异常
  validation.py   数值输入解析与对称性相对容差校验
  kernel.py       Householder 三对角化 + 隐式移位 QL (被测计算内核)
  evidence.py     残差 / 正交性 / 重构证据, 重特征值子空间(主角)比较
  reference.py    独立参考: SciPy(LAPACK) 与 mpmath 任意精度
  service.py      编排: 校验 → 内核 → 证据 → 质量门 → 失败分类
  trace.py        关联 request_id 的结构化步骤/失败/不确定结论日志
  schemas.py      FastAPI 请求/响应模型
  api.py          FastAPI 接口 (/healthz, /eigendecompose)
tests/
  fixtures.py     确定性本地合成夹具 (显式谱构造, 真值独立于内核)
  test_*.py       50 个单元/集成测试, 断言具体数值与失败类别
demo.py           本地演示脚本 (无需起服务)
```

## 安装与运行

```bash
pip install -r requirements.txt

# 运行全部测试
python3 -m pytest tests/ -v

# 本地演示 (对角/重复谱/近退化/尺度悬殊 + 三类失败分类)
python3 demo.py

# 启动服务
uvicorn eigenservice.api:app --host 127.0.0.1 --port 8000
# 或: python -m eigenservice.api
```

### 请求示例

```bash
curl -s -X POST http://127.0.0.1:8000/eigendecompose \
  -H 'Content-Type: application/json' \
  -H 'X-Request-ID: my-req-001' \
  -d '{"matrix": [[2,1],[1,2]]}'
```

请求体可携带 `request_id`（或用 `X-Request-ID` 头）以及逐配置覆盖项
（`max_size`、`sym_tol`、`residual_tol`、`orthogonality_tol`、
`reconstruction_tol`、`gap_tol`、`base_sweeps`、`sweep_multiplier`）。

## 算法与可配置预算

1. **Householder 三对角化**：逐列反射把对称阵 `A` 化为三对角 `T`，
   累积正交阵 `Q`，满足 `A = Q T Qᵀ`。
2. **隐式 Wilkinson 移位 QL**：对 `T` 迭代，Givens 旋转同步作用于累积
   特征向量；按相对机器精度判据逐特征值 deflation。
3. 迭代预算 `max_sweeps = max(base_sweeps, sweep_multiplier·n)`，
   **随规模可配置**。预算耗尽时内核返回 `converged=False`，服务层据此
   抛出 `not_converged`，并把部分结果作为"不确定结论"单列。

## 后验证据（不是只看迭代是否停止）

- **残差**：`||Avⱼ − λⱼvⱼ||`（整体 Frobenius、相对 `||A||_F`、逐特征对）。
- **正交性**：`||VᵀV − I||_F` 及最大元偏差。
- **重构**：`||V diag(λ) Vᵀ − A||_F`。
- **重特征值按子空间比较**：先按特征值间隙聚类，再用
  `σ(Q₁ᵀQ₂)` 的主角比较特征子空间；单特征值才比较带符号自由度的向量。
  因此重复谱不会因"向量在子空间内任意旋转"而被误判。
- **输入对称性按相对容差**：`||A−Aᵀ||_F ≤ sym_tol · ||A||_F`。

## 错误语义

| HTTP | `error_code` | 触发条件 | 结论 |
|------|--------------|----------|------|
| 422 | `invalid_matrix` | 非方阵 / 含 NaN、Inf / 规模越界 / 不可解析 | 拒绝输入 |
| 422 | `asymmetric_matrix` | 相对容差下不满足对称（附实测相对不对称度） | 拒绝输入 |
| 409 | `not_converged` | 迭代预算耗尽仍有特征值未收敛 | **失败**，部分结果列入 `uncertainties`，不返回成功 |
| 409 | `quality_check_failed` | 已收敛但残差/正交性/重构证据超阈值 | 失败，证据列入 `failures` / `uncertainties` |
| 500 | `internal_error` | 未预期错误 | 失败 |

成功响应含 `success=true`、特征值（升序）、特征向量（按行返回，第 j 行对应
第 j 个特征值）、`quality` 证据与 `trace`；错误响应含 `error_code`、
`error_message`、`details` 与同结构 `trace`。

## 可解释性

每个响应（成功或失败）都带：

- `request_id`：调用方提供或服务生成，贯穿响应与日志；
- `service_version` / `core`：版本与处理实现标识；
- `trace.steps`：关键步骤（接收 → 校验 → 内核开始/结束 → 证据评估 →
  质量门）及相对耗时；
- `trace.failures`：失败原因（稳定 code + 消息 + 数值细节）；
- `trace.uncertainties`：**不确定结论单列**，例如预算耗尽时的部分特征值
  与当时残差。

## 验证案例与独立参考

测试用确定性本地合成数据，无任何生产账号或外部业务数据：

- **对角矩阵**：零次 QL 移位，特征向量即标准基；
- **重复谱**（三重 2.0 / 二重 −1.5）：按子空间核对，重数正确；
- **近退化**（间隙 1e-10）：双精度只分辨簇，簇整体对照高精度真值；
- **尺度悬殊**（1e6 / 1 / 1e-6）：以逐对残差与正交性为准；
- **随机稠密对称阵**：全量交叉核对。

参考答案**不由被测内核产生**：

- `scipy.linalg.eigh`（LAPACK `dsyevd`）双精度对照；
- `mpmath.mp.eigsy`（任意精度，默认 80 位十进制）独立高精度真值；
- 多数夹具由显式谱 `Q diag(λ) Qᵀ` 构造，特征值真值就是构造参数本身。

测试断言具体数值（误差上界、主角、重数、重构误差）和具体失败类别，
而非"接口能调通"。当前覆盖率约 98%。

## 复现一次完整核验

```bash
pip install -r requirements.txt
python3 -m pytest tests/ -v          # 50 passed
python3 demo.py                       # 数值对照 + 失败分类 + 轨迹
uvicorn eigenservice.api:app          # 起服务后用上方 curl 验证
```
