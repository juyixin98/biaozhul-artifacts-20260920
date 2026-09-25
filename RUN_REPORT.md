# 运行报告（RUN_REPORT）

本文件如实记录在本机实际执行过的命令与结果。开发过程中出现过的失败也一并记录，
并区分"代码缺陷修复"与"测试预期修正"。没有未修复的已知失败项。

## 1. 环境

| 项 | 值 |
|---|---|
| 操作系统 | Linux 6.8.0-90-generic |
| Python | 3.12.3 |
| NumPy | 2.5.3 |
| pytest | 9.1.1（含 pytest-cov） |
| 外部模型/数据 | 无，全部合成数据 |

## 2. 自动化测试

命令：

```bash
python3 -m pytest -v
python3 -m pytest --cov=drift -q
```

最终结果：**66 passed**（无失败、无跳过），行覆盖率 **95%**：

```
Name                 Stmts   Miss  Cover
----------------------------------------
drift/__init__.py        3      0   100%
drift/__main__.py       11      1    91%
drift/binning.py       108      2    98%
drift/metrics.py       143      6    96%
drift/pipeline.py       17      0   100%
drift/service.py       221     19    91%
drift/synthetic.py      45      1    98%
----------------------------------------
TOTAL                  548     29    95%
```

覆盖的验收场景：

- 同分布合成数据 → PSI 小；平移分布 → PSI 显著增大；
- 空桶：`none` 平滑 PSI=`inf`，`laplace`/`floor` 得有限大值；
- 当前窗口全缺失、空窗口；小样本（n=10）标记 `small_sample`；
- 缺值率漂移、极端值进溢出桶、基线常值退化、基线全缺失拒绝建档；
- HTTP 全部端点与 4xx/422/404 路径（真实端口、真实 JSON，而非 mock）。

### 首轮测试的失败项与处置（如实记录）

首次跑全量测试时为 **58 passed / 5 failed**，逐项定位：

1. `test_floor_smoothing_renormalizes`（测试预期问题）
   误以为"抬底 ε 再重归一化"后每桶仍 ≥ ε。数学上重归一化会把被抬高的桶
   压回 ε 以下。已修正断言为"所有桶严格为正且各自求和为 1"，文档表述同步。
2. `test_psi_is_infinite_when_one_side_has_empty_bin`（测试预期问题）
   `[1,0] vs [0.5,0.5]` 实际只有 1 个桶单侧为空，期望的 inf 桶数从 2 改为 1。
3. `test_overflow_bins_participate_in_psi`（测试预期问题）
   取频率数组时误用 `[-2]`；`to_dict()` 的频率数组不含缺值桶，溢出桶是 `[-1]`。
4. `test_shifted_without_smoothing_is_finite...`（测试设计问题）
   固定 min/max 建箱时，基线两侧溢出桶结构上恒为 0，因此真实管线在 `none`
   平滑下只要当前有越界值就必然 `inf`——原测试假设不成立。改为两个测试：
   手工构造双侧同支撑计数验证有限 PSI 路径；真实越界数据验证 `inf` + 提示。
5. `test_mismatched_bins_rejected`（**真实代码缺陷**）
   `FixedBins` 是含 `np.ndarray` 的 dataclass，`baseline.bins != current.bins`
   触发 NumPy 歧义真值错误，而非预期的 ValueError。已在 `metrics.py`
   新增 `_same_bins()` 用 `np.array_equal` 做显式比较。

此外在测试前的库冒烟阶段还发现并修掉两个真实缺陷：
`assign_counts` 重构时遗留 `missing_idx` 未定义变量（NameError）；
`js_divergence` 误写 `float("e")`（ValueError）；以及在代码评审环节自查发现
"右边界闭合"逻辑会把严格大于基线最大值的有限值错放进内部桶（已改为仅
"恰好等于 edges[-1]"才闭合，严格更大者进溢出桶，并加了回归测试）。

修复后 64 → 66 个用例全部通过（新增 2 个 CLI 用例）。

## 3. 库 API 演示

命令：`python examples/demo_library.py`（实际输出节选）：

```
=== 1. 同分布 N(0,1) vs N(0,1) ===
  PSI=0.0272  JS=0.0048  TVD=0.0528  band=little_drift_rule_of_thumb
=== 2. 平移分布 N(0,1) vs N(1,1) ===
  PSI=1.0731  JS=0.1702  TVD=0.4203  band=severe_drift_rule_of_thumb
=== 3. 空桶场景 / smoothing=none ===
  PSI=inf  note: 未做平滑且存在一侧空桶，PSI=inf；可改用 laplace/floor ...
=== 3. 空桶场景 / smoothing=laplace ===  PSI=4.4290
=== 3. 空桶场景 / smoothing=floor ===    PSI=6.9036
=== 4. 当前窗口全缺失 ===
  PSI=2.2197  missing cur=1.000 delta=+1.000  current_all_missing=True
  note: ...频率由平滑规则构造，PSI 仅反映"信息缺失"的极端假设，不能解读为真实分布漂移
=== 5. 小样本（n=10） ===
  PSI=0.7720  small_sample=True
  note: 观测数低于启发式下限 30，指标方差大，阈值打标不构成统计结论
=== 6. 缺值率漂移（当前 40% 缺失） === PSI=0.0314, missing delta≈+0.394
=== 7. 极端值进入溢出桶（10%） === PSI=0.5433, overflow=20, underflow=28
```

值得注意的真实结果：**场景 5 两窗口同分布、仅因 n=10，PSI 就达 0.77**
（超过"严重漂移"经验阈值）。这正是不能把阈值当统计证明的直接例证，
服务在 `notes` 中明确标注了这一点。

可复现性核验（同种子两次独立运行）：

```
psi run1 = 1.073078971625
psi run2 = 1.073078971625
identical: True
```

## 4. HTTP 服务实测

启动：

```bash
python3 -m drift --host 127.0.0.1 --port 8000
# 特征漂移监测服务已启动: http://127.0.0.1:8000
curl -s http://127.0.0.1:8000/healthz
# {"status": "ok", "service": "feature-drift"}
```

`POST /v1/monitor`（样例文件由 `python examples/generate_examples.py` 离线生成）：

| 请求文件 | 场景 | 实测 PSI | band |
|---|---|---|---|
| `monitor_same.json` | 同分布 | 0.02718 | little_drift_rule_of_thumb |
| `monitor_shifted.json` | 平移 +1σ | 1.07308 | severe_drift_rule_of_thumb |
| `monitor_missing.json` | 当前约 30% 缺值 | 0.02766（missing delta=0.296） | little_drift_rule_of_thumb |
| `monitor_no_smoothing.json` | 平移 + none 平滑 | `"inf"`（JSON 字符串） | severe_drift_rule_of_thumb |

建档 → 查询 → 漂移两阶段流程：

```bash
curl -s -X POST .../v1/baselines -d @examples/baseline_create.json
# -> baseline_id（实测为 99a98932e80542529eac7208d08675eb，随机生成）
curl -s .../v1/baselines/<id>      # n_total=2000, counts 长度 13（10+2+1）
curl -s -X POST .../v1/drift -d @/tmp/drift_req.json
# -> psi = 1.0731, severe_drift_rule_of_thumb，与一次性 monitor 完全一致
```

`POST /v1/demo` 各场景实测：

```
same          psi=0.0272   band=little_drift_rule_of_thumb
shifted       psi=1.0731   band=severe_drift_rule_of_thumb
all_missing   psi=2.2197   small=True  all_missing=True
small_sample  psi=0.772    small=True  all_missing=False
extremes      psi=0.2556   band=severe_drift_rule_of_thumb
```

错误处理实测状态码：

| 输入 | 状态码 | 响应要点 |
|---|---|---|
| 未知路径 `/nope` | 404 | 未知路径 |
| 非法 JSON | 400 | JSON 解析失败 |
| baseline/current 特征集合不一致 | 400 | 要求特征集合完全一致 |
| 数组含布尔值 | 400 | 只接受数值或 null |
| 基线特征全为 null | 422 | 无法建档：没有任何有限非缺失值 |
| drift 使用不存在的 baseline_id | 404 | 基线不存在 |
| 未知 smoothing | 400 | 必须是 none/laplace/floor |
| n_bins=-3 | 400 | n_bins 必须是 >= 1 的整数 |

测试结束后已停止服务并确认 8000 端口释放。

## 5. 复现步骤汇总

```bash
pip install -r requirements.txt
python3 -m pytest -q                      # 66 passed
python3 examples/demo_library.py          # 库 API 七场景
python3 examples/generate_examples.py     # （可选）重新生成请求样例
python3 -m drift --host 127.0.0.1 --port 8000   # 起服务
# 另一终端：
curl -s -X POST http://127.0.0.1:8000/v1/monitor \
  -H 'Content-Type: application/json' -d @examples/monitor_shifted.json
```

## 6. 明确不作为结论的事项

- 未把任何 PSI 阈值表述为"统计证明/显著性"；字段命名、README、响应
  `disclaimer`、`notes` 四处统一为"rule of thumb（工程经验规则）"口径。
- 未做前端。未下载任何外部模型或数据。未实现分类特征、持久化与窗口调度
  （README"已知边界"已列明）。
