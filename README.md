# abseq — 固定样本量 A/B 指标分析与顺序偏差模拟

纯后端、仅依赖 NumPy 的本地分析工具，用于**固定样本量（fixed-horizon）**的离线 A/B
指标分析：输入组别与观测值，输出均值差与预先设定置信水平的置信区间。

## 明确的边界（非目标）

- **不支持"随时窥视后停止"的推断有效性。** 置信区间仅在样本量事先固定时有效；
  反复查看结果并提前停止会使名义覆盖率失效。每次分析结果都附带该警告，
  `peeking` 模拟用于量化这种顺序偏差（type-I 错误膨胀）。
- **不提供任何业务决策承诺。** 输出仅为统计量与区间，是否采纳由使用方自行判断。
- 不下载外部模型或数据；所有验证均基于固定种子的合成数据。
- 无前端。交互方式为 Python 库、命令行和本地 JSON HTTP 服务。

## 安装与运行

```bash
pip install -r requirements.txt        # 运行依赖：numpy
pip install -r requirements-dev.txt    # 测试依赖：pytest
```

### 作为库

```python
from abseq import analyze, analyze_groups, make_synthetic

groups, observations = make_synthetic(500, 500, effect=0.0, seed=42)
result = analyze_groups(groups, observations, confidence=0.95,
                        missing_strategy="drop")
print(result["mean_difference"], result["ci_lower"], result["ci_upper"])
```

### 命令行

```bash
python -m abseq coverage --n-control 500 --n-treatment 500 --reps 2000 --seed 42
python -m abseq peeking  --n-per-group 500 --looks 5 --reps 2000 --seed 42
```

### 本地 HTTP 服务（仅标准库 + NumPy）

```bash
python -m abseq.server --host 127.0.0.1 --port 8000
bash examples/curl_examples.sh        # 需先启动服务；可用 BASE=... 覆盖地址
```

| 方法 | 路径 | 说明 |
|------|------|------|
| GET  | `/health`      | 健康检查 |
| POST | `/v1/analyze`  | 固定样本量 A/B 分析（`control`/`treatment` 两样本，或 `groups`/`observations` 分组形式） |
| POST | `/v1/coverage` | 蒙特卡洛覆盖率模拟 |
| POST | `/v1/peeking`  | 顺序窥视 type-I 膨胀模拟 |

请求/响应样例见 `examples/*.json`。统一响应信封：`{"ok": bool, "data": ..., "error": ...}`。

## 方法说明

- **区间**：Welch t 区间（不假设方差齐性），自由度用 Welch–Satterthwaite 公式。
  为保持零 SciPy 依赖，t 分布 CDF 由闭式 PDF 梯形积分计算，分位数用二分法求逆；
  正态分位数用 Acklam 有理逼近。测试中对 df=1/10/30/1000 的 0.975 分位数与
  标准 t 表核对（误差 < 1e-3）。
- **缺失值策略**（非有限值 NaN/±inf 视为缺失，逐组独立处理）：
  - `raise`（默认）：发现缺失即报错；
  - `drop`：剔除缺失值（完全随机缺失假设下的完整个案分析）；
  - `impute_mean`：用组内观测均值填充。**注意**：这会低估组内方差、使区间偏窄，
    结果中会附带 `imputation_note` 提示；下方验收结果可见其实证欠覆盖。
- **可复现性**：所有随机性经 `numpy.random.default_rng(seed)`，同种子结果逐位一致
  （有测试保证）。

## 验收运行记录（实际执行，2026-09-25）

环境：Python 3.12.3，NumPy 2.5.3，pytest 9.1.1，Linux。

### 自动化测试

```
$ python3 -m pytest tests/ -q
...............................................                          [100%]
47 passed in 34.59s
```

**未通过项记录**：首轮运行 `47 passed` 之前有 `1 failed`——
`test_server.py::TestServer::test_analyze_groups_with_missing`。原因是测试夹具
构造不当：control 组 2 条观测中 1 条缺失，`drop` 后仅剩 1 条，触发了"每组至少
2 条观测"的合法校验（服务正确返回 400）。实现无误，修正测试数据（control 组
3 条观测含 1 条缺失）后全部通过。

### 覆盖率验收（零效应合成数据，名义 95%，reps=2000，seed=42）

| # | 场景 | 命令要点 | 经验覆盖率 | 平均区间宽 |
|---|------|----------|-----------|-----------|
| 1 | 均衡 500:500，无缺失 | `coverage --n-control 500 --n-treatment 500` | **0.9415** | 0.497 |
| 2 | 均衡，10% 缺失，`drop` | `... --missing-strategy drop --missing-rate 0.1` | **0.9435** | 0.524 |
| 3 | 均衡，10% 缺失，`impute_mean` | `... --missing-strategy impute_mean --missing-rate 0.1` | **0.9150** | 0.471 |
| 4 | 极不均衡 100:10000 | `coverage --n-control 100 --n-treatment 10000` | **0.9540** | 0.794 |

解读（2000 次重复下 95% 名义覆盖的蒙特卡洛标准误约 ±0.5pp）：

- 场景 1、2、4 与名义 95% 一致（在蒙特卡洛误差范围内）；极不均衡样本下
  Welch 区间仍保持覆盖。
- 场景 3 显示 `impute_mean` **欠覆盖（91.5%）**：均值填充压缩了组内方差、
  区间偏窄。这是该策略的已知偏差，工具在输出中以 `imputation_note` 明示，
  默认策略因此为 `raise` 而非静默填充。

### 顺序偏差（窥视）验收（零效应，alpha=0.05，reps=2000，seed=42）

| # | 场景 | 至少一次名义显著的比例 |
|---|------|------------------------|
| 5 | 5 次等间隔窥视（n=500/组） | **0.1415** |
| 6 | 仅 1 次查看（合法固定视界） | **0.0570** |

结论：在真实零效应下，5 次窥视把"至少一次显著"的概率从名义 5% 膨胀到约
14.2%，而单次固定视界检验保持在名义水平附近（5.7%，在蒙特卡洛误差内）。
这正是本工具拒绝支持随时窥视后停止的原因。

### 服务冒烟测试

`python -m abseq.server --port 8931` 后执行 `bash examples/curl_examples.sh`
（`BASE=http://127.0.0.1:8931`）：`/health`、`/v1/analyze`（两种输入形式）、
`/v1/coverage`、`/v1/peeking` 均返回 `200` 且 `ok: true`；覆盖率与窥视端点
结果与上方 CLI 运行逐位一致（同种子）。首次尝试端口 8123 因本机占用失败
（`OSError: [Errno 98] Address already in use`），换用 8931 后成功。

## 项目结构

```
abseq/
  __init__.py      # 公共 API
  stats.py         # Welch t 区间、t/正态分位数（NumPy + 标准库实现）
  missing.py       # 缺失值策略：raise / drop / impute_mean
  analysis.py      # 固定样本量分析入口（含有效性警告）
  synthetic.py     # 固定种子合成数据
  simulation.py    # 覆盖率与窥视 type-I 膨胀模拟
  server.py        # 本地 JSON HTTP 服务（仅标准库）
  __main__.py      # CLI：coverage / peeking
examples/          # 请求样例 JSON 与 curl 脚本
tests/             # 47 个 pytest 用例
```

## 限制

- 区间为 Welch t 近似：中等以上样本量表现良好，小样本且强非正态时可能欠覆盖。
- 缺失处理假设完全随机缺失（MCAR）；非随机缺失下 `drop`/`impute_mean` 均有偏。
- 模拟仅覆盖正态、等方差、独立观测情形，不外推至其他数据生成过程。
- 输出不构成任何业务决策建议。
