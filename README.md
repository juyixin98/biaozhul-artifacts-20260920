# 实验指标顺序偏差 — 固定样本量离线 A/B 指标分析工具

纯后端 Python + NumPy 项目：本地机器学习基础设施服务的一个最小组成件——
对**预先固定样本量**的 A/B 实验做离线均值差分析（Welch 置信区间），并用
可复现的合成数据验证其核心统计机制。不下载任何外部模型或数据。

## 明确的有效性边界（重要）

- 本工具**仅适用于样本量与置信水平在查看数据之前就已固定**的离线分析。
- **不支持**"随时窥视（peeking）后停止"的序贯决策：边累积数据边反复计算
  置信区间、一旦显著就停止，会使实际假阳性率显著高于名义 α。本工具对此
  场景**不提供有效性保证**，响应中也始终附带该提示。
- 本工具只提供统计计算，**不构成任何业务决策建议**。

## 功能

- 输入两组的原始观测（JSON 数组，`null` 表示缺失），输出：
  处理组 − 对照组的均值差、Welch 双侧置信区间、各组均值/标准差/有效样本量、
  标准误、Welch–Satterthwaite 自由度、缺失值计数。
- 缺失值策略：
  - `drop`（默认）：按组删除缺失观测（complete-case）；
  - `impute_mean`：用本组观测均值填补。**注意**：该策略低估方差，模拟显示
    其覆盖率明显低于名义水平（见下文实测结果），仅用于敏感性分析。
- 覆盖率自检：在零假设（两组同分布、真实差异为 0）下用固定种子的合成
  数据做蒙特卡洛模拟，验证置信区间的经验覆盖率是否接近名义水平。
- t 分布分位数为纯标准库实现（不完全贝塔函数 + 二分求逆），**不依赖 SciPy**；
  精度由公开 t 分布表数值的单元测试保证。

## 安装与运行

环境：Python 3.12，NumPy 2.5（唯一第三方依赖），pytest 用于测试。

```bash
pip install -r requirements.txt

# 1) 启动本地服务（仅标准库 HTTP，无 Web 框架）
python -m abtest.server --host 127.0.0.1 --port 8000

# 2) 请求样例（另见 examples/curl_examples.sh）
curl -s -X POST http://127.0.0.1:8000/analyze \
  -H "Content-Type: application/json" \
  --data @examples/analyze_request.json

# 3) 覆盖率自检（固定种子，可复现）
python -m abtest.simulate_cli --simulations 2000 --confidence 0.95 --seed 20260922

# 4) 自动化测试
python -m pytest
```

### 请求格式（POST /analyze）

```json
{
  "control":   [1.02, 0.98, 1.10, null],
  "treatment": [1.08, 1.12, 1.05, 1.15],
  "confidence_level": 0.95,
  "missing_strategy": "drop"
}
```

`confidence_level` 与 `missing_strategy` 可选，默认 `0.95` 与 `"drop"`。
响应为分析结果各字段，外加 `validity_notice`（固定样本量有效性提示）。
另有 `GET /health` 存活探针。

## 实测记录（如实报告）

以下命令在本机（Python 3.12.3，NumPy 2.5.3，pytest 9.1.1）实际执行。

### 自动化测试

```
$ python -m pytest
...............................................                          [100%]
47 passed in 12.02s
```

47 项测试全部通过，无跳过、无失败。覆盖：t 分布实现对照公开 t 表、
Welch 区间手算对照、缺失值两种策略、极不均衡样本（50 vs 5000）、
固定种子可复现性、覆盖率校准、HTTP 层（含真实 socket 往返与 400/404）。

### 覆盖率模拟（零假设下 2000 次重复，种子 20260922，名义覆盖率 0.95）

蒙特卡洛标准误 ≈ √(0.95·0.05/2000) ≈ 0.0049。

| 场景 | 经验覆盖率 | 判定 |
|---|---|---|
| 均衡样本（500 vs 500），无缺失 | 0.9530 | 与 0.95 一致（误差范围内） |
| 极不均衡（50 vs 5000），无缺失 | 0.9400 | 与 0.95 一致（约 2σ 内） |
| 20% 缺失，`drop` 策略 | 0.9475 | 与 0.95 一致 |
| 20% 缺失，`impute_mean` 策略 | **0.8800** | **显著低于名义水平，未通过校准** |

`impute_mean` 覆盖率不足是预期行为（均值填补人为压低组内方差），
已在文档与代码注释中明确标注为仅限敏感性分析，不作为默认策略。
除此之外无未通过项。

### 服务端实测

```
$ curl -s http://127.0.0.1:8931/health
{"status": "ok"}

$ curl -s -X POST .../analyze --data @examples/analyze_request.json
{"mean_control": 1.01, "mean_treatment": 1.0811, "mean_diff": 0.0711,
 "ci_lower": 0.0250, "ci_upper": 0.1172, "confidence_level": 0.95,
 "n_control": 9, "n_treatment": 9, ..., "validity_notice": "Fixed-sample-size
 analysis only. This interval is NOT valid if you peeked ..."}

$ curl -s -X POST .../analyze --data '{"control": [1.0], "treatment": [1.0, 2.0]}'
{"error": "each group needs at least 2 valid observations after missing-value
 handling, got control=1, treatment=2"}   # HTTP 400
```

## 项目结构

```
abtest/
  __init__.py        # 包入口与范围声明
  tdist.py           # 纯标准库 Student-t CDF/分位数（不完全贝塔函数）
  stats.py           # Welch 均值差置信区间（核心分析）
  missing.py         # 缺失值策略：drop / impute_mean
  simulate.py        # 固定种子的覆盖率蒙特卡洛模拟
  simulate_cli.py    # 覆盖率自检命令行
  server.py          # 标准库 HTTP JSON 服务（POST /analyze, GET /health）
examples/
  analyze_request.json
  curl_examples.sh
tests/               # 47 项 pytest 测试
requirements.txt
pytest.ini
```

## 设计取舍

- **Welch 而非 Student 双样本 t**：不假设方差齐性，对极不均衡样本更稳健
  （模拟中 1:100 场景覆盖率仍校准）。
- **不引入 SciPy**：t 分位数自实现并以公开 t 表数值做回归测试，减少依赖。
- **不引入 Web 框架**：服务层用 `http.server`，请求校验逻辑
  （`build_response`）为纯函数，可脱离服务器直接测试。
- **不提供序贯/多重比较校正**：超出本工具的固定样本量范围，刻意不实现，
  避免被误用于窥视场景。
