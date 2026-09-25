# 请求样例

本目录提供两套等价的、可直接运行的样例，覆盖全部验收场景。

## 文件说明

| 文件 | 作用 |
|------|------|
| `spec.json` | 列定义（ColumnSpec 列表）：age / income / city / plan_tier |
| `train.json` | 训练数据（6 行，含缺值 `null`；`plan_tier` 恒为 7，零方差） |
| `request_shuffled_unknown.json` | 推理请求：**列顺序故意打乱**，第 0 行是训练中未见的城市 `Hangzhou`，含缺值 |
| `python_api_example.py` | 不依赖文件的库 API 示例 |
| `pipeline.json` | 运行 `fit` 后生成的带校验和模型产物 |
| `response.json` | 运行 `transform` 后生成的真实响应 |

## 场景要点（请求 → 响应）

- **列错序**：请求按 `plan_tier, city, income, age` 给出，输出仍严格按
  `age, income, city=..., plan_tier` 的训练 schema 排列。
- **未知类别**：第 0 行 `city="Hangzhou"` 在训练中不存在，三个城市独热位全 0，
  末尾 `city=__unknown__` 标志位为 1；`unknown_categories.city=[true,false,false]`。
- **缺值填充**：`age` 的 `null` 用训练均值填充后标准化为约 0；`income` 的 `null`
  用常量 0 填充后参与标准化。
- **零方差**：`plan_tier` 训练值恒为 7，scale 安全回退为 1，输出列全 0。

## 运行

```bash
# CLI
python3 -m feature_pipeline fit --spec spec.json --data train.json --out pipeline.json
python3 -m feature_pipeline transform --model pipeline.json \
  --data request_shuffled_unknown.json --out response.json
cat response.json

# 库 API（在仓库根目录执行）
python3 examples/python_api_example.py
```

> 注：CLI 在严格 schema 模式下会拒绝请求里出现的多余键，因此数据文件只保留
> 真正的特征列；场景说明放在本文件而非 JSON 内。
