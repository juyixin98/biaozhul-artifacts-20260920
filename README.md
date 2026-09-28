# Sparse SPD Symbolic + Numeric LDLᵀ Backend

稀疏对称正定（SPD）矩阵的**符号分解**与**数值分解**后端。符号结构（消元
顺序、消元树、L 的精确非零模式、填充量）先于数值计算产生；数值层在符号
预分配的位置上做左视（left-looking）稀疏 `L D Lᵀ` 分解与三角求解；
另有独立的误差证据层（残差、稀疏重构、LAPACK、mpmath 高精度）和
FastAPI 服务接口。

所有数据均为**本地合成夹具**，无外部账号或真实业务数据。

---

## 1. 目录与模块关系

```
app/
  config.py                 阈值/规模/精度配置（环境变量可覆盖）
  errors.py                 分类错误码（input/numerical/service/internal）
  runlog.py                 run_id + 输入指纹 + 版本 + 步骤的结构化日志
  numerical_input/
    sparse_matrix.py        COO 校验（索引/重复/对角/对称性），存为上三角 CSR
    fixtures.py             网格5点 / 带状 / 箭头 / 模式变更 / 非正定 / 奇异
  core/
    ordering.py             消元顺序：natural / minimum_degree / nested_dissection
    symbolic.py             符号分解：精确 L 列模式（左视布尔并集）+ 消元树
    numerical.py            数值 LDLᵀ 左视列分解 + 前/回代（置换同步左右端）
  evidence/
    metrics.py              残差、LDLᵀ 重构、稠密 LAPACK、mpmath 高精度对照
  service/
    engine.py               编排全流程 + 符号结构缓存（仅模式完全相同才复用）
    schemas.py              FastAPI/Pydantic 请求响应模型
  main.py                   HTTP 接口
tests/                      独立 oracle 与具体断言（见下）
scripts/demo.py             本地验证/报告脚本 -> reports/verification.json
```

数据流：

```
COO ─校验→ 上三角CSR ─排序→ perm
                          ├→ 符号分解（L列模式/消元树/填充统计，可缓存复用）
                          └→ B=PAPᵀ ─数值LDLᵀ→ L,D ─求解(右端同步置换/解逆置换)
                                                          └→ 证据层独立核验
```

## 2. 算法假设

- 输入为实对称矩阵，COO 给全或给上/下三角均可，但**不允许重复坐标**
  （避免静默求和造成模式歧义），且每个对角元必须显式给出。
- 分解采用 **LDLᵀ**：`P A Pᵀ = L D Lᵀ`，L 单位下三角、D 对角。对一般
  SPD 无需开平方；非正定性通过**主元 D[k]** 直接暴露。
- **置换同步两端**：求解时先 `b_perm = P b`，解得 `x_perm` 后 `x = Pᵀx_perm`。
- 符号结构复用的唯一条件是 **(维度, 上三角稀疏模式指纹, 排序方法) 完全相同**；
  仅数值不同而坐标一致可复用，任何结构变化（哪怕 nnz 相同）都强制重建。
- **不全量稠密化**：校验、排序、符号、数值全部走稀疏索引/集合；只有证据层
  在受限规模（默认 n≤2000 稠密、n≤60 mpmath）下用稠密参考，且仅用于对照。
- 主元判据：`|D[k]| ≤ pivot_tol·max|diag|` 判为 **SINGULAR_PIVOT**；
  `D[k] < 0` 判为 **NON_SPD_PIVOT**，并同时给出消元位置 k 与**原始索引**。

## 3. 依赖版本（本机实测）

| 包 | 版本 |
|----|------|
| Python | 3.12.3 |
| numpy | 2.4.6 |
| scipy | 1.15.3 |
| mpmath | 1.3.0 |
| fastapi | 0.141.1 |
| uvicorn | 0.54.0 |
| pydantic | 2.13.5 |
| pytest | 9.1.1 |
| httpx | 0.28.1（TestClient） |

安装：`pip install -r requirements.txt`

## 4. 本地验证命令与预期判断

### 4.1 单元/端到端测试（46 个）

```bash
python3 -m pytest -q
```

预期：全部 **passed**。覆盖：

- `test_input_validation`：越界/负索引/重复坐标/缺对角/结构与数值不对称/
  非有限值/长度不符——断言**具体错误码**；
- `test_symbolic`：L 列模式与**独立稠密布尔消元 oracle**逐列一致；三对角
  自然序零填充；连通网格消元树单根；最小度确实降低填充；
- `test_numerical`：D 对角与**独立教科书稠密 LDL** 一致；解与 **LAPACK** 一致；
  `LDLᵀ = PAPᵀ` 稀疏重构；右端随置换；L 单位对角；
- `test_failure_cases`：负对角/不定（主元恰为 −3）/零 Schur 主元，断言
  失败**类别**与**原始主元索引**；
- `test_reuse`：同模式换值复用、换模式不复用、排序方法不串用、维度不同不复用；
- `test_evidence`：残差/重构/LAPACK/**mpmath(50dps)** 一致；填充排序报告；
- `test_api`：健康检查、solve/factor/ordering-compare/fixtures，非正定返回
  分类错误而非 500，坏 JSON 返回 422。

参考解不来自被测核心：oracle 为独立稠密布尔消元、`numpy.linalg.solve`
（LAPACK）、独立教科书 LDL 公式、mpmath 高精度 Cholesky。

### 4.2 本地验证脚本（夹具总览 + 排序填充报告 + JSON）

```bash
python3 scripts/demo.py
echo "exit=$?"
```

预期：SPD 夹具 `evidence_passed=True`、`residual_rel≈1e-16`；非正定夹具
打印对应错误码且 `match=True`；退出码 `0`。结果写入
`reports/verification.json`。

### 4.3 启动服务并手测

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000
# 另开终端
curl -s localhost:8000/health | python3 -m json.tool
curl -s -X POST localhost:8000/api/v1/fixtures/run \
  -H 'content-type: application/json' \
  -d '{"name":"grid","ordering":"minimum_degree"}' | python3 -m json.tool
```

交互式 API 文档：<http://127.0.0.1:8000/docs>

预期非正定请求返回：

```json
{"status":"error","error_code":"NON_SPD_PIVOT","error_category":"numerical",
 "details":{"pivot":1,"original_index":1,"pivot_value":-3.0,...}}
```

异常/未知状态一律走 `error` 分支（分类失败 HTTP 200 带错误体，未预期异常
HTTP 500 且记录 run 日志），不会被统一返回成功。

## 5. 日志

每次运行生成 `run_id` 与输入 SHA-256 指纹，记录库版本与各计算步骤
（`run_start → ordering_done → symbolic_done → factor_progress … →
numeric_done → evidence`）。输出到 stdout 与 `logs/runs.log`，可据
`run_id`/`fingerprint` 关联具体输入。

## 6. 排序与填充效果（示例，40×40=1600 五点网格）

| ordering | nnz(L) | fill | ratio |
|----------|-------:|-----:|------:|
| natural | 64039 | 59319 | 13.57 |
| minimum_degree | 21504 | 16784 | 4.56 |
| nested_dissection | 23844 | 19124 | 5.05 |

最小度/嵌套分割显著降低填充；具体数值以 `scripts/demo.py` 实测为准。
