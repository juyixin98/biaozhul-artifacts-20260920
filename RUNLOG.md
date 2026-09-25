# 运行记录（RUNLOG）

环境：Linux 6.8.0-90-generic，Python 3.12.3，NumPy 2.5.3。
日期：2026-09-24。所有命令均在项目根目录执行。

## 1. 环境确认

```
$ python3 --version && python3 -c "import numpy; print('numpy', numpy.__version__)"
Python 3.12.3
numpy 2.5.3
```

## 2. 自动化测试

```
$ python3 -m unittest discover -s tests -v
...
Ran 65 tests in 0.152s
OK
```

**最终结果：65 个用例全部通过，无跳过、无失败。**

### 开发过程中出现并已修复的失败（如实记录）

首次运行 `python3 -m unittest discover -s tests -v` 时 **65 个用例中 8 个失败/错误**，
逐项修复后复跑通过：

1. `StreamingISTFT._drain` 消费前缀后帧仍按绝对坐标写入 → `IndexError: boolean index
   did not match`（`test_pcm_job`、`test_short_inputs_job`、`test_synthetic_job...` 等 5 个
   ERROR）。修复：缓冲区改为“活动后缀 + 绝对基准 `_base`”，`finish` 内部重组
   `take_ready` 已弹出的前缀，保证返回完整信号。
2. `test_hann_half_hop_cola_constant` / `test_squared_weight_strictly_positive_half_hop`
   FAIL：测试断言区间写错——有限缓冲区边界 `[0, hop)` 与 `[L-n_fft, L)` 本就是权重
   斜坡/零区，断言改为只检查内部区 `[hop, L-n_fft)`。
3. `test_center_false_reconstructs_interior` FAIL：`center=False` 时周期 Hann 的
   `w[0]=0` 使第 0 个样本本征零权重（真实行为，非 bug）。测试改为承认该位置并
   只比较其余样本；同时 `istft(center=False)` 的诊断改为只覆盖“有帧到达”的区域，
   并新增 `uncovered_tail_length` 字段。
4. `test_nfft_equals_hop_rect` FAIL：hop=n_fft 无重叠时，补边区内不被任何完整帧
   覆盖的尾部样本权重为 0（真实边界行为）。测试改用 hop 整倍数长度（512）。
5. `test_hann_half_hop_nola_holds` FAIL：`cola_diagnostic` 的周期 NOLA 检查用了有限
   缓冲区 `overlap_sums`，边界零被误判为本征零。修复：改为在一个 `n_fft` 周期上做
   循环重叠累加（`_periodic_overlap_sum`），与无限格点定义一致。
6. `test_diagnostic_job_flags_zero_weights` FAIL：诊断测试断言的零权重位置写错
   （center 补边后帧起点映射到信号位置 32、96、…，而非 0、64、…）。修正断言。
7. `test_short_inputs_job` ERROR：空信号（n=0）时 `np.max` 作用在空数组上。修复：
   服务中整段 vs 分块差异计算对空数组返回 0.0。
8. 测试辅助函数误用 `prepare_config(..., window=...)`（参数名应为 `window_name`）→
   `TypeError`。修正测试。

另有一处交付前修正：`spectrum_*.npy` 最初用 `ndarray.tofile` 写原始字节却叫 `.npy`，
命名误导，已改为 `np.save` 标准格式（`np.load` 验证通过）。

## 3. 请求样例运行

### 3.1 合成信号（`examples/request_synthetic.json`）

n_fft=256, hop=128, hann, center, 长度 8000，分块 `[100, 200, 37]`。

```
$ python3 -m stft_service.service examples/request_synthetic.json
exit=0
```

结果（report.json 摘要）：

- 帧数：整段 63 = 分块 63 = 期望 63（match=true）
- `forward_whole_vs_stream_max_abs` = 0.0（分块与整段频谱逐位一致）
- `inverse_whole_vs_stream_max_abs` = 0.0；单帧推送流式逆变换同样 = 0.0
- 重建误差（整段与分块相同）：max_abs = 4.44e-16，RMSE = 1.12e-16，SNR ≈ 312.6 dB
- `reconstruction.possible` = true，零权重位置为空，min_weight ≈ 0.2624

### 3.2 本地 PCM（`examples/request_pcm.json`）

输入文件由以下命令生成（s16，3000 样本，300/1200/2500 Hz + 噪声）：

```
$ python3 - <<'EOF'
from stft_service.signals import synthesize
from stft_service.pcm_io import write_pcm
x = synthesize(3000, sample_rate=8000, frequencies=(300.0,1200.0,2500.0),
               amplitudes=(0.5,0.3,0.15), noise_std=0.005)
write_pcm("examples/input_s16.pcm", x, "s16")
EOF
wrote examples/input_s16.pcm samples: 3000 bytes: 6000

$ python3 -m stft_service.service examples/request_pcm.json
exit=0
```

结果：n_fft=128, hop=64；帧数 47 = 47（match=true）；分块 300；
重建 max_abs = 1.86e-15，RMSE = 9.92e-17，SNR ≈ 312.7 dB；
整段 vs 分块前向/反向差异均为 0.0；NOLA 成立。
另产出 `reconstructed_whole.s16.pcm`（整型量化重建）。

### 3.3 不可重建诊断（`examples/request_diagnostic.json`）

故意配置：周期 Hann，`hop = n_fft = 64`（无重叠）。

```
$ python3 -m stft_service.service examples/request_diagnostic.json
exit=0
```

结果（按设计：计算照常完成，诊断明确，不静默除零）：

- `reconstruction.possible` = **false**
- `diagnostic.nola_holds` = false，`nola_zero_positions_periodic` = [0]
- `diagnostic.cola_holds` = false（无重叠 Hann 的 COLA 和为 0）
- 输出信号零权重位置：32, 96, 160, …, 992（共 16 个，即补边裁剪后各帧起点）
- 这些位置输出为 0，其余位置仍精确重建；整段 vs 分块逆变换差异 = 0.0
- 整体指标：RMSE = 0.0636，max_abs = 0.798（恰为零权重位置处的原始幅度）

### 3.4 非法参数（错误路径）

```
$ python3 -m stft_service.service /tmp/badreq.json   # n_fft=16, hop_length=17
{
  "status": "error",
  "error": "hop_length (17) must be <= n_fft (16); larger hops leave unreconstructable gaps"
}
exit=2
```

## 4. 输出文件核对

```
$ ls examples/out_synthetic
magnitude_whole.npy  original.pcm  reconstructed_stream.pcm  reconstructed_whole.pcm
report.json  spectrum_preview.csv  spectrum_stream.npy  spectrum_whole.npy

$ python3 -c "import numpy as np; a=np.load('examples/out_synthetic/spectrum_whole.npy'); \
b=np.load('examples/out_synthetic/spectrum_stream.npy'); \
print(a.shape, b.shape, np.max(np.abs(a-b)))"
(63, 129) (63, 129) 0.0
```

## 5. 未通过项 / 遗留限制

- 当前无未通过的测试（65/65 通过）。
- 已知限制（属设计边界，非缺陷，README 已注明）：
  - 流式前向仅支持 `pad_mode="constant"`；`reflect` 需整段信号，构造时显式报错。
  - `center=False` 时信号尾部不足一帧的样本不重建（诊断 `uncovered_tail_length`）。
  - 周期 Hann + `center=False` 时第 0 个样本本征零权重（分析窗在该点为零且无重叠覆盖）。
  - PCM 为无文件头原始格式，不解析 WAV 容器。
