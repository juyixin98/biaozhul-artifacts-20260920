# 特征变换流水线（Feature Transformation Pipeline）

纯后端、**仅依赖 Python + NumPy** 的本地机器学习预处理基础设施服务。
不下载任何外部模型或数据，使用固定随机种子的**可复现合成数据**和简单确定性算法验证核心机制。无前端。

## 能力概览

- **可拟合的标准化**：`StandardScaler`，训练时学习均值与总体标准差；零方差列安全回退（scale=1，输出全 0，不除零）。
- **类别编码**：`OneHotEncoder`，类别在训练时排序固定；推理时对**未知类别**输出全 0，并可选追加 `__unknown__` 标志列（`indicator`）或忽略（`ignore`）。
- **缺值填充**：`SimpleImputer`，数值列支持 `mean` / `median` / `constant`，类别列支持 `most_frequent`（平局按字典序确定性打破）/ `constant`。
- **流水线**：每列固定顺序 `填充 -> 标准化/独热`；`fit()` 只在训练数据上学习状态，`transform()` **只读**使用冻结状态，永不重拟合、永不修改入参。
- **Schema 与列顺序**：输入按声明 schema 校验（缺列 / 多列 / 行数不一致 / 非一维 / 类型不符都会快速失败）；**输入列任意乱序都会被重排回固定输出顺序**，结果与规范顺序完全一致。
- **序列化**：保存为带 **SHA-256 校验和**的 JSON，显式记录 schema、每列处理步骤的**顺序**与拟合参数、输出特征名；加载时校验格式与校验和，篡改/损坏即拒绝。保存→加载后的推理结果与原流水线逐元素一致。

## 环境要求

- Python 3.12（3.9+ 亦可）
- NumPy（开发验证版本 `2.5.3`）
- 运行测试需 pytest + pytest-cov（开发验证版本 `pytest 9.1.1` / `coverage 7.16.1`）

```bash
pip install -r requirements.txt       # 运行时：numpy
pip install -r requirements-dev.txt   # 测试：  + pytest, pytest-cov
```

## 目录结构

```
.
├── feature_pipeline/
│   ├── __init__.py        # 公共 API
│   ├── exceptions.py      # 类型化错误（Schema/Fitting/NotFitted/Serialization）
│   ├── transformers.py    # SimpleImputer / StandardScaler / OneHotEncoder
│   ├── pipeline.py        # ColumnSpec / FeaturePipeline / TransformResult
│   ├── serialization.py   # 带校验和的 JSON 保存/加载
│   ├── cli.py             # fit / transform / demo 命令行
│   ├── demo.py            # 可复现合成数据端到端演示
│   └── __main__.py
├── examples/              # 可直接运行的请求样例（库 API + CLI）
├── tests/                 # pytest 自动化测试（50 个）
├── requirements.txt / requirements-dev.txt
└── pyproject.toml
```

## 快速开始

### 方式一：命令行（JSON 进 / JSON 出）

数据文件为「列名 -> 值列表」的 JSON 对象，缺值用 `null`；spec 文件为 ColumnSpec 列表。

```bash
# 1) 在训练数据上拟合并保存
python3 -m feature_pipeline fit \
  --spec examples/spec.json \
  --data examples/train.json \
  --out examples/pipeline.json

# 2) 对新数据推理（请求里列顺序故意打乱、含未知城市 Hangzhou 与缺值）
python3 -m feature_pipeline transform \
  --model examples/pipeline.json \
  --data examples/request_shuffled_unknown.json \
  --out examples/response.json
```

### 方式二：Python 库 API

```bash
python3 examples/python_api_example.py
```

```python
import numpy as np
from feature_pipeline import ColumnSpec, FeaturePipeline, save_pipeline, load_pipeline

specs = [
    ColumnSpec("age", "numeric", impute_strategy="mean", scale=True),
    ColumnSpec("city", "categorical", impute_strategy="most_frequent",
               handle_unknown="indicator"),
]
pipe = FeaturePipeline(specs).fit({       # 仅训练数据参与 fit
    "age":  np.array([20.0, 40.0, np.nan]),
    "city": np.array(["NY", "SF", "NY"], dtype=object),
})

result = pipe.transform({                 # 列乱序也会被重排；只读推理
    "city": np.array(["ZZZ", "NY"], dtype=object),  # ZZZ = 未知类别
    "age":  np.array([100.0, np.nan]),
})
print(result.X)                            # 数值特征矩阵
print(result.unknown_categories)           # 每个类别列的未知行掩码

save_pipeline(pipe, "pipeline.json")       # 保存顺序 + schema + 状态（带校验和）
same = load_pipeline("pipeline.json")      # 加载后推理结果逐元素一致
```

### 方式三：合成数据端到端演示

```bash
python3 -m feature_pipeline demo --outdir demo_output
```

固定种子 `SEED=20260925`，生成 200 行训练 / 6 行测试，自动断言：
训练-测试隔离、未知类别、列乱序一致、零方差全 0、保存/加载一致，并把样例
请求/响应写入 `demo_output/`。

## 序列化产物

`pipeline.json` 顶层字段：

| 字段 | 含义 |
|------|------|
| `format` / `version` | 产物格式标识与版本 |
| `schema` | 声明的列定义，**按顺序** |
| `columns[].steps` | 每列处理步骤，**按执行顺序**（如 `impute` → `standard_scale` / `one_hot_encode`）及拟合参数 |
| `feature_names_out` | 输出特征名固定顺序 |
| `checksum` | 对上述字段规范化 JSON 的 SHA-256；加载时重算比对，不一致即拒绝 |

## 验收点对照

| 验收要求 | 实现方式 | 对应测试 |
|----------|----------|----------|
| 测试数据不参与拟合 | 状态仅在 `fit()` 学习；`transform()` 不更新任何 `_` 状态，并断言前后不变 | `test_test_data_does_not_reaffect_fitted_state...`、`test_standard_scaler_uses_fitted_mean_and_std` |
| 未知类别 | 未知值独热全 0 + 可选标志列，返回未知行掩码 | `test_one_hot_unknown_category...`、CLI/demo 用例 |
| 列错序 | schema 校验后按声明顺序重排，乱序与规范顺序输出逐元素相等 | `test_shuffled_columns...`、`test_loaded_pipeline...shuffled` |
| 零方差 | std=0 时 scale 回退 1，输出全 0，无除零/告警 | `test_standard_scaler_zero_variance...`、`test_zero_variance_numeric_column...` |
| 保存/加载一致 | 校验和签名 + 完整状态恢复，输出与特征名一致 | `test_save_load_roundtrip...`、demo 与示例断言 |

## 运行测试与结果

命令：

```bash
python3 -m pytest -q --cov=feature_pipeline --cov-report=term-missing
```

**实际运行结果（2026-09-25，Python 3.12.3 / NumPy 2.5.3 / pytest 9.1.1）：**

```
50 passed in 0.60s
TOTAL  535 stmts   13 miss   98% cover
```

各模块覆盖率：`pipeline.py 100%`、`demo.py 100%`、`exceptions.py 100%`、
`__init__.py 100%`、`cli.py 99%`、`transformers.py 97%`、`serialization.py 95%`。
未覆盖的 13 行为抽象基类的 `NotImplementedError` 占位方法与畸形产物的兜底异常分支，
属防御性冗余。总覆盖率 **98% ≥ 80%** 要求。

开发过程中先写测试确认 RED（导入失败），再实现转 GREEN；期间修复的真实问题：
独热编码默认带未知标志列导致的维度期望修正、`transform` 返回 `TransformResult`
而非裸数组的测试断言、序列化未拟合流水线需抛 `SerializationError`。

**未通过项：无。** 最终 50 个测试全部通过；CLI、库 API、demo 三种方式均实际运行成功。

## 设计约定与限制

- 标准差使用**总体标准差**（ddof=0）。
- 仅支持一维列输入；严格模式下多余列直接报错（防止静默丢特征）。
- 类别以字符串形式比较；缺值统一识别为 `None` 或浮点 `NaN`。
- 序列化采用人类可读 JSON（无第三方依赖、无 pickle 反序列化风险），但产物未加密——不含密钥，仅做完整性校验。
- 不含模型训练/推理服务（HTTP）、不做前端；定位为可嵌入上层服务的本地库 + CLI。
