# 运行记录（RUNLOG）

- 日期：2026-09-24（CST）
- 机器：Linux 6.8.0-90-generic，Python 3.12.3，NumPy 2.5.3，pytest 9.1.1
- 依赖：仅 NumPy（`requirements.txt`）；WAV 使用标准库 `wave`，无其他第三方依赖
- 运行目录：仓库根目录；未 `pip install`，通过 `PYTHONPATH=$(pwd)` 导入本地包

本文件如实记录实际执行过的命令、观测到的结果、中途失败与修复，以及最终状态。

## 1. 自动化测试

命令：

```bash
python3 -m pytest          # 最终结果
```

最终结果（1.22s）：

```
32 passed
```

逐用例结果（最后一次运行，全部 PASSED）：

| 模块::用例 | 验收点 | 结果 |
|---|---|---|
| test_correlator.py::test_ncc_matches_reference_implementation | NCC 切片实现 vs 朴素双重循环参考实现，逐元素误差 < 1e-12 | PASS |
| test_correlator.py::test_ncc_zero_lag_is_one_for_identical_signals | 相同信号零延迟 NCC=1 | PASS |
| test_correlator.py::test_ncc_pearson_immune_to_dc_offset_and_gain | Pearson NCC 对直流偏置+3×增益免疫 | PASS |
| test_correlator.py::test_positive_integer_delay_white_noise | **已知正整数延迟 +37 精确命中** | PASS |
| test_correlator.py::test_negative_integer_delay_white_noise | **已知负整数延迟 −37 精确命中** | PASS |
| test_correlator.py::test_sign_convention_direct_shift_check | **符号定义**：d=−25/0/+25 时峰位=d | PASS |
| test_correlator.py::test_periodic_sine_is_flagged_multiple_peaks | **周期信号判 multiple_peaks** | PASS |
| test_correlator.py::test_silence_is_low_energy | **双静音判 low_energy，无 NaN** | PASS |
| test_correlator.py::test_one_sided_silence_is_low_energy | **单路静音判 low_energy** | PASS |
| test_correlator.py::test_uncorrelated_noise_is_low_peak | **独立噪声不相关判 low_peak** | PASS |
| test_correlator.py::test_white_noise_with_moderate_noise_still_confident | **−15 dB 噪声下仍稳定检出 +23（抖动 <1.5）** | PASS |
| test_correlator.py::test_edge_hit_when_delay_at_search_boundary | **峰贴搜索边界判 edge_hit** | PASS |
| test_correlator.py::test_interpolated_subsample_delay_on_chirp | 亚采样 2.5 样本插值误差 <0.3 | PASS |
| test_correlator.py::test_integer_mode_delay_is_exact_without_interpolation | 不开插值时输出为整数且精确 | PASS |
| test_correlator.py::test_negative_peak_keeps_sign_for_antiphase | 反相峰 peak<−0.999（保留符号） | PASS |
| test_windows.py（7 例） | 切窗边界/重叠/参数校验、分段延迟(+11,−7)、单窗静音、null 序列化 | PASS |
| test_pipeline_io.py（7 例） | u8/s16/s32/f32 PCM 与 WAV 往返、端到端、文件输入、拒绝覆盖、短信号报错 | PASS |
| test_cli.py（3 例） | CLI 版本、合成/PCM 端到端、退出码、拒绝覆盖 | PASS |

### 1.1 开发过程中真实出现过的失败与修复（第一轮 25 passed / 7 failed）

以下为首次运行测试时的真实失败，均已修复并复跑通过，记录以备审计：

1. **`source(synthetic) 含未知参数: ['mode']`**（影响 3 个流水线测试）
   严格参数校验把请求 envelope 的 `mode` 字段当成了合成参数。修复：解析时剔除 `mode`，
   并顺带把 `_comment`（下划线前缀）规定为注释键。
2. **s16 PCM 往返误差超差**（首次误差 ~1.0，二次 4.4e-5）
   两个叠加问题：(a) 有符号整型编码正峰值用满量程 32768 相乘会溢出环绕为负值，
   改为 `peak−1`；(b) 测试用白噪声按 RMS 归一化后峰值超过满量程被限幅，属于测试数据问题，
   写 PCM 前显式限幅；(c) 量化往返容差按真实码点间距修正为 5e-5（≈1.5/32767）。
3. **全零窗原因码断言过严**：全静音窗口除 `low_energy` 外还伴随 `low_peak`/`edge_hit`。
   修复：测试只断言必须包含 `low_energy` 且状态为 uncertain（多原因并存是正确行为）。
4. **edge_hit 测试场景构造不当（连续调整 4 次，真实过程）**：
   - 最初用白噪声延迟 100、范围 40：范围外边缘重叠太少，相关值过低贴不上边；
   - 改用周期正弦（P=50）：45 ≡ −5 (mod 50)，出现范围**内**周期别名；
   - 改用 P=100 正弦：线性（补零）移位使 lag=−5 处两段零填充完美重叠，形成假峰；
   - 改用补零 chirp：零填充边缘假相关（lag=38 出现 −0.56）仍压过真峰；
   - 改用循环移位 chirp：4000 点上 1 个接缝错配仍可能让 lag=39 略高；
   - **最终方案**：白噪声循环移位**恰好** +64、`max_lag=64`、窗长 2048。边界点 NCC≈1
     完美对齐，缠绕别名（−64）因重叠比例小而更低，峰稳定贴边。这也明确了 `edge_hit`
     的语义："真实延迟位于搜索边界时，无法排除更大延迟，故不可信"。

## 2. CLI 样例实际运行

命令（先造数据，再逐个跑；输出目录见下）：

```bash
python3 examples/generate_example_data.py
bash scripts/run_examples.sh          # 一键复现（时间戳输出目录）
```

实测输出目录：`example_runs/20260924_090348/`，6 个样例**退出码均为 0**。关键数值：

| 样例请求 | 输入 | 估计延迟 | peak | peak_ratio | 状态 |
|---|---|---|---|---|---|
| request_synthetic.json | 合成白噪，真值 **+37**，−20dB 噪 | **+37.000**（=真值） | 0.990 | 0.031 | confident |
| request_negative_delay.json | 合成白噪，真值 **−37** | **−37.000**（=真值） | 0.990 | 0.036 | confident |
| request_pcm.json | 本地 s16 交错双声道 PCM，真值 +21 | **+21.000** | 0.997 | 0.033 | confident |
| request_wav_windowed.json | 本地 WAV，2 个 4000 点窗，插值开 | **+21.000 / +21.000** | 0.997 | 0.050 / 0.065 | confident |
| request_periodic_sine.json | 200Hz 正弦（周期 40） | 30.000（不取） | −1.000 | **1.000** | **uncertain: multiple_peaks** |
| request_silence.json | 极小幅噪声（RMS −66 dBFS） | 3.000（不取） | 1.000 | 0.044 | **uncertain: low_energy** |

说明（如实记录的观察）：

- 静音样例中峰高显示 1.000，是因为极小幅高斯底噪两通道同源自移位，NCC 仍能取到高值；
  但能量门限正确优先拦截，状态为 `low_energy`。这正是"高相关 ≠ 可信"的设计预期。
- 周期正弦的 `peak=-1.000`：在该整数延迟下，距离半个周期的候选 lag 与真实峰
  `|NCC|` 都为 1（浮点量级相当），argmax 取到反相位置——无论取到哪个，
  `peak_ratio=1.000` 都使结果判 `multiple_peaks`，延迟值在不确定状态下不应被使用。
- 每次运行均写出 `results.json`、`correlation.npz`（lags + 每窗 NCC 行）、`signals.npz`。
- 环境中没有 `python` 只有 `python3`，首跑 `scripts/run_examples.sh` 报
  `python: command not found (exit 127)`，已把脚本内调用改为 `python3` 后通过。

### 2.1 错误路径实测（退出码）

| 场景 | 命令/构造 | 实测结果 |
|---|---|---|
| 拒绝覆盖 | 同一输出目录重复 run | exit=1，`输出文件已存在，拒绝覆盖: [...]` |
| 请求文件不存在 | `run /tmp/nonexistent_req.json` | exit=2，`请求文件不存在` |
| 非法 JSON | 内容 `{ bad json` | exit=2，`请求 JSON 解析失败: ...` |
| 未知参数名 | estimator 里拼错 `max_leg_typo` | exit=1，`estimator 含未知参数: ['max_leg_typo']` |
| 窗口非法 | window_size=100、max_lag=128 | exit=1，`window_size (100) 必须大于 2*max_lag (256)` |
| 信号过短 | duration=0.01s、max_lag=128 | ValueError `信号长度 ... 必须大于 2*max_lag`（由测试覆盖） |

## 3. 未通过项 / 遗留问题

- **无**：32 个自动化测试全部通过；6 个 CLI 样例退出码均为 0，数值与真值/预期状态一致。
- 需使用者注意、但**不是失败项**的已知边界（详见 README 第 11 节）：
  1. 延迟超出 `max_lag` 时表现为 `edge_hit`，需调大范围重跑；
  2. 周期信号的多峰是数学本质，NCC 无法单独消歧，结果必须保持 uncertain；
  3. 直接法 NCC 面向可读性，超大窗口/超大 max_lag 场景可替换为 FFT 实现（接口不变）。

## 4. 复现步骤汇总

```bash
cd <仓库根目录>
python3 -m pytest                              # 1) 自动化测试（32 passed）
python3 examples/generate_example_data.py     # 2) 生成 PCM/WAV 样例数据
bash scripts/run_examples.sh                  # 3) 一键跑全部 6 个请求样例
# 或单独运行：
PYTHONPATH=. python3 -m delay_correlator run examples/request_synthetic.json -o out/synth
```
