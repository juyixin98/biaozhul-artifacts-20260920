# 特征变换流水线（Feature Transformation Pipeline）

纯后端的本地机器学习基础设施服务。用 **Python + NumPy** 实现可拟合的
**标准化（standardize）、类别编码（one-hot）、缺值填充（impute）** 流水线，
训练时拟合状态，推理时只读使用；工件（artifact）以 JSON 序列化，显式保存
**列顺序与 schema**。不下载任何外部模型或数据，用可复现的合成数据和一个
NumPy 最小二乘线性模型验证核心机制。无前端。

## 设计约束的落实方式

| 约束 | 实现 |
|------|------|
| 测试数据不参与拟合 | 合成数据 train/test 用独立 RNG 子流生成；`Pipeline.fit` 只在训练行上调用；填充/标准化统计量只来自训练数据，推理缺失值用训练均值/中位数/众数填充 |
| 未知类别 | `OneHotEncoder` 仅学习训练类别；推理遇到未见过的类别编码为**全零向量**（不报错、不新增列） |
| 列错序 | 流水线**按列名取值**、按 schema 固定顺序输出；输入键顺序任意、允许多余键，特征矩阵逐元素相同 |
| 零方差 | `StandardScaler` 检测 `std <= 1e-12`，置 `scale=1.0`，该列变换结果全为 `0.0`，不抛除零错误；模型用 `lstsq` 容忍秩亏 |
| 保存/加载一致 | JSON 原子写入；加载时校验 kind/version，并交叉校验 schema、特征名与模型维度 |

## 目录结构

```
feature_pipeline/
  schema.py      # ColumnSpec / Schema，列顺序与校验
  transforms.py  # NumericImputer / CategoricalImputer / StandardScaler / OneHotEncoder
  pipeline.py    # 按列有序的 fit/transform，按名取列、按序输出
  model.py       # numpy.linalg.lstsq 线性回归（含截距，容忍秩亏）
  artifact.py    # schema + pipeline + model 的 JSON 序列化/一致性校验
  service.py     # JSON 文件 CLI 服务：train / infer
  data.py        # 可复现合成数据（含缺值/零方差/未知类别/错序）
  errors.py
tests/           # 77 个 pytest 用例（AAA 结构）
examples/        # 请求/响应样例与端到端 demo
```

## 安装

需要 Python ≥ 3.10、NumPy。当前环境：Python 3.12.3 / NumPy 2.5.3。

```bash
pip install -r requirements.txt          # numpy + pytest + pytest-cov
# 或只装运行依赖：pip install numpy
```

## 快速开始（CLI）

训练（请求为 JSON 文件，无网络端口）：

```bash
python -m feature_pipeline.service train \
  --request examples/train_request.json \
  --artifact artifacts/model.json \
  --report artifacts/train_report.json
```

推理（列顺序可任意；可含未知类别、缺值）：

```bash
python -m feature_pipeline.service infer \
  --artifact artifacts/model.json \
  --request examples/infer_request.json \
  --output artifacts/predictions.json
```

### 请求格式

训练请求：

```json
{
  "schema": {
    "columns": [
      {"name": "age",    "dtype": "numeric",     "impute": "mean"},
      {"name": "income", "dtype": "numeric",     "impute": "median"},
      {"name": "constant_col", "dtype": "numeric",
       "impute": "constant", "fill_value": 7.0},
      {"name": "city",   "dtype": "categorical", "impute": "mode"}
    ],
    "target": "price"
  },
  "rows":    [ {"age": 25.0, "income": 4200.0, "constant_col": 7.0, "city": "north"} ],
  "targets": [ 248.0 ]
}
```

推理请求（注意第二行键顺序被打乱、城市为训练中未见的 `"remote"`）：

```json
{ "rows": [
  {"age": 31.0, "income": 5100.0, "constant_col": 7.0, "city": "north"},
  {"city": "remote", "constant_col": 7.0, "income": 4600.0, "age": 27.0},
  {"age": null, "income": null, "constant_col": 7.0, "city": "east"}
] }
```

字段说明：

- `dtype`：`numeric` 或 `categorical`。
- numeric `impute`：`mean`（默认）/`median`/`constant`；
  categorical `impute`：`mode`（默认）/`constant`。
- `constant` 策略必须提供 `fill_value`。
- 缺值写法：`null`、`NaN`、空字符串（numeric）；`null`/`NaN`（categorical）。
- numeric 列变换链：`缺值填充 -> 标准化`；categorical 列：`缺值填充 -> one-hot`。

完整样例：[`examples/train_request.json`](examples/train_request.json)、
[`examples/infer_request.json`](examples/infer_request.json)、
[`examples/infer_response.sample.json`](examples/infer_response.sample.json)、
[`examples/train_report.sample.json`](examples/train_report.sample.json)。

## 快速开始（Python API）

```python
from feature_pipeline import ModelArtifact            # 或从子模块导入
from feature_pipeline.data import make_synthetic_dataset

ds = make_synthetic_dataset(seed=20260925)            # 可复现
artifact = ModelArtifact.fit(ds["schema"], ds["train_rows"], ds["train_targets"])
pred = artifact.predict_rows(ds["test_rows"])         # 只读推理
artifact.save("artifacts/model.json")
loaded = ModelArtifact.load("artifacts/model.json")   # 保存/加载一致
```

端到端演示（逐一打印四个验收场景）：

```bash
python examples/demo.py
```

## 序列化格式

工件为人类可读 JSON，顶层顺序固定：

```
kind / version / schema / feature_names / pipeline / model
```

- `schema.columns`：按拟合顺序记录每列名字、类型、填充策略；
- `feature_names`：展开 one-hot 后的输出特征布局（如 `city=north`）；
- `pipeline.steps`：**按 schema 列顺序**保存每列有序的、已拟合变换器及其参数
  （均值/中位数/众数、mean/std/scale、类别词表等）；
- `model`：截距、系数、特征数与秩。

加载时若 kind/version 不符、schema 与流水线不符、存储特征名与实际输出不符、
或模型维度与流水线输出维度不符，会抛出 `ArtifactError`。

## 运行测试

```bash
python -m pytest                       # 全部用例
python -m pytest --cov=feature_pipeline --cov-report=term-missing
```

### 如实记录的运行结果

以下均为本机实际执行输出（Python 3.12.3 / NumPy 2.5.3 / pytest 9.1.1）。

**单元 + 集成测试：`77 passed`，覆盖率 `94%`（要求 ≥80%）。**

各文件覆盖率：

| 文件 | 覆盖率 |
|------|--------|
| `schema.py` | 90% |
| `transforms.py` | 93% |
| `pipeline.py` | 95% |
| `model.py` | 93% |
| `artifact.py` | 96% |
| `service.py` | 96% |
| `data.py` | 96% |
| `errors.py` / `__init__.py` | 100% |

`python examples/demo.py` 关键输出（验收点逐条核对）：

```
unknown category present in test but not train: unknown in train_cities=False, in_test=True
'remote' rows: 1, one-hot block all zeros: True
canonical vs shuffled-key matrices identical: True
zero_variance detected: True, stored scale: 1.0
complete test rows: 42, RMSE: 1.5532 (noise std 0.5)
max |pred_before - pred_after|: 0.000e+00
```

CLI train/infer 实际成功（退出码 0），推理输出见
`examples/infer_response.sample.json`。负向用例：推理行缺列时返回
`error: row 0 is missing columns: ['constant_col', 'income']`，退出码 `1`。

### 开发过程中遇到并修复的真实问题（如实记录）

1. **NumPy 定长字符串截断**：初版用 `np.array(["north", ..., "remote"])`，
   数组被推断为 `<U5`，6 字符的 `"remote"` 被静默截断成 `"remot"`，导致“未知
   类别”断言失败。修复为显式 `dtype=object`，并新增未知类别全零向量的测试。
2. **含缺值行 RMSE 偏高（18.5）**：目标由真实值生成，而特征缺失后被均值/中位数
   填充，这部分误差是缺值填充的**不可约误差**（训练集同样存在，train RMSE≈11.9），
   非流水线 bug。因此把“泛化能力”断言改为在**完整且类别已知**的测试行上检验
   （仅含 0.5 噪声，实测 RMSE 1.55），并另加测试保证缺值行仍输出有限预测值。
3. **未知类别行可能被缺值注入覆盖**：保护注入的未知类别行不被置空。

未通过项：截至最后一次运行，**无未通过测试**（77/77 通过）。

## 范围与限制

- 服务为本地 JSON 文件 CLI，刻意不提供 HTTP 端口（题目要求纯后端、本地基础设施）。
- 模型仅为验证机制的 OLS 线性回归，不做正则化/分类/评估指标库。
- 仅支持本文件记录的两种 dtype 与三种填充策略；大规模数据未做分块/流式优化。
