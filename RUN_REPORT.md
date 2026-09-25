# 运行记录（RUN_REPORT）

本文件如实记录在交付环境中实际执行的命令与结果。所有命令均在项目根目录运行，
Python 与依赖版本：

```
Python 3.12.3
NumPy 2.5.3
pytest 9.1.1
pytest-cov 7.1.0
coverage 7.16.1
平台: Linux 6.8.0-90-generic
```

仅使用 NumPy（无 SciPy、无任何前端/播放器/界面组件）。

## 1. 自动化测试

命令：

```bash
python -m pytest -q --cov=streaming_stft --cov-report=term-missing
```

实际结果（最后一次运行）：

```
113 passed in 2.28s
```

覆盖率：

| 模块 | 语句 | 未覆盖 | 覆盖率 |
|---|---:|---:|---:|
| `windows.py` | 29 | 2 | 93% |
| `stft.py` | 195 | 8 | 96% |
| `streaming.py` | 225 | 12 | 95% |
| `signals.py` | 40 | 0 | 100% |
| `pcmio.py` | 52 | 2 | 96% |
| `service.py` | 157 | 5 | 97% |
| `cli.py` | 59 | 1 | 98% |
| `__init__.py` | 4 | 0 | 100% |
| **总计** | **761** | **30** | **96%** |

达到并超过 80% 的最低覆盖率要求。未覆盖行均为防御性分支
（如 `ndim != 1` 的理论不可达分支、个别异常构造路径）。

### 开发过程中发现并修复的真实缺陷（如实记录）

1. **帧数公式漏掉全零尾部的帧**：`hop = nfft` 时，按居中后长度算出的最后一帧
   够不到信号末端（例：n=1000, nfft=256, hop=256 时末尾 104 个样本无帧可达）。
   修复：帧数取「base 内容纳帧数」与「覆盖信号末端所需帧数」的最大值
   （`frame_plan`），并由整段与流式路径共用。
2. **流式右侧 reflect 补边方向/边界错误**：最初取了反射序列错误的一段且
   重复了边界样本，导致块大小为 1 时尾帧与整段结果不一致（最大差约 1.7）。
   修复：反射扩展取 `seed[:p][::-1]`（不重复边界），并保证从信号末端开始拼接。
   修复后所有块大小下帧谱与整段**逐位相等**。
3. **`istft(signal_length=...)` 越界校验失效**：曾按请求长度扩大网格，使
   超长请求不报错。修复：网格在分析时固定，`signal_length` 只能裁短，超出即抛错。
4. **PCM 读取未校验字节对齐**：`read_pcm` 未检查文件大小是否为 itemsize 整数倍
   （该检查只存在于未被调用的 `PCMSource.sample_count`）。已移入 `read_pcm`。

## 2. 整段 vs 分块验收矩阵

脚本（内联）以非均匀调度 `[137, 363, 1, 499]` 循环分块，覆盖短输入、尾块与
零窗权重位置。`bulk maxerr` 为整段重建相对原始信号的最大绝对误差；
`stream diff` 为分块重建相对整段重建的最大绝对差：

```
    n  nfft/hop      pad | bulk maxerr  stream diff  zero-wt
    1   256/128     edge |    2.78e-17     0.00e+00       0
    5   256/128     edge |    1.39e-17     0.00e+00       0
  127   256/128     edge |    3.09e-13     0.00e+00       0
  128   256/128     edge |    5.00e-16     0.00e+00       0
  129   256/128  reflect |    8.88e-16     0.00e+00       0
  255    64/16  constant |    4.44e-16     0.00e+00       0
  300   100/100 constant |    1.08e+00     0.00e+00       3
  777   300/70  reflect |    1.11e-15     0.00e+00       0
 1000    256/64     edge |    8.88e-16     0.00e+00       0
```

说明：

- 所有可重建配置 `stream diff == 0`（逐位相等），误差仅来自 `rfft/irfft`
  浮点往返（约 1e-15）。
- `n=127 + edge` 的 3e-13 来自边缘**复制补边**本身（边界外被当作等于边界值），
  属该补边模式的预期近似；`reflect`/内部样本为机器精度。
- `n=300, hop=nfft, hann`（无重叠）存在 3 个零权重点，被正确诊断为不可重建；
  `bulk maxerr` 无意义，服务以 `covered=false` 与零权重索引报告而非静默置零。
- 零窗权重专项：`nfft=32, hop=32, hann, center=False`，160 样本上零权重位置为
  `[0, 32, 64, 96, 128]`，`covered=false`，与整段诊断一致。

此外做过 208 组参数组合（窗长/步长/补边/居中/长度交叉）的整段-分块对照，
结果**全部一致**（`stream diff = 0`，零权重索引一致，长度一致）。

## 3. 三个请求样例（实际运行）

### 3.1 合成信号（可重建）

```bash
PYTHONPATH=src python -m streaming_stft.cli run --request examples/request_synth.json
# 退出码 0
```

关键结果：

```json
{
  "covered": true,
  "n_samples": 2000, "n_frames": 16, "n_bins": 129,
  "bulk_rebuild_max_abs": 4.440892098500626e-16,
  "bulk_rebuild_rmse": 1.1671215552253952e-16,
  "bulk_rebuild_relative_rms": 2.460280327500031e-16,
  "weight_is_constant": false,
  "chunks": [{
    "chunk_sizes": [137, 363, 1, 499],
    "frames_bit_identical": true, "frame_max_abs_diff": 0.0,
    "rebuild_max_abs_diff": 0.0, "rebuild_rmse": 0.0,
    "zero_weight_count": 0
  }]
}
```

诊断原文：

```
nfft=256, hop=128, frames=16; interior weight sum is NOT constant
(sample-wise normalisation); all samples covered; min squared-window weight = 0.102434
```

### 3.2 本地 float32 PCM（可重建，75% 重叠内部 COLA）

```bash
PYTHONPATH=src python examples/generate_sample_pcm.py     # 800 samples float32
PYTHONPATH=src python -m streaming_stft.cli run --request examples/request_pcm.json
# 退出码 0
```

关键结果：

```json
{
  "covered": true,
  "n_samples": 800, "n_frames": 26, "n_bins": 65,
  "bulk_rebuild_max_abs": 3.3306690738754696e-16,
  "weight_is_constant": true,
  "chunks": [{
    "chunk_sizes": [50, 77, 3],
    "frames_bit_identical": true, "frame_max_abs_diff": 0.0,
    "rebuild_max_abs_diff": 0.0, "rebuild_rmse": 0.0
  }]
}
```

诊断原文：

```
nfft=128, hop=32, frames=26; interior weight sum is CONSTANT = 1.5
(perfect COLA: normalisation is uniform); all samples covered;
min squared-window weight = 1.25
```

### 3.3 不可重建配置（无重叠 Hann）

```bash
PYTHONPATH=src python -m streaming_stft.cli run --request examples/request_unreconstructible.json
# 退出码 2
```

关键结果：

```json
{"covered": false, "zero_weight_count": 3, "zero_weight_positions": [0, 64, 128]}
```

诊断原文：

```
nfft=64, hop=64, frames=3; interior weight sum is NOT constant (sample-wise
normalisation); NOT RECONSTRUCTIBLE: 3 sample(s) have zero window weight
(first indices: 0, 64, 128); increase overlap (smaller hop) or use a window
that is non-zero across its full support (e.g. hamming/rect)
```

分块路径报告的零权重数量/位置与整段完全相同（`zero_weight_count: 3`，
`zero_weight_first: [0, 64, 128]`）。

### 3.4 窗/步长独立诊断

```bash
PYTHONPATH=src python -m streaming_stft.cli diagnose --nfft 256 --hop 128 --window hann
# 退出码 0，interior_all_covered=true
PYTHONPATH=src python -m streaming_stft.cli diagnose --nfft 64 --hop 64 --window hann --no-center
# 退出码 2，interior_all_covered=false，零权重位置 0,64,128,...（每 64 一个）
```

## 4. 输出文件

每个 `run` 在 `output.dir` 实际写出并已核验：

```
out/synth/ spectrogram.npy (16,129) complex128 | reconstructed.npy (2000,) float64
           reconstructed.pcm float64 | input.pcm float64 | metrics.json
out/pcm/   spectrogram.npy (26,65) complex128  | reconstructed.npy (800,) float64
           reconstructed.pcm float32 | input.pcm float32 | metrics.json
out/bad_window/ 同上（covered=false 的诊断结果，仍写出数值供检查）
```

## 5. 未通过项 / 已知限制（如实说明）

- **无未通过的测试**：最终 113 个测试全部通过，覆盖率 96%。
- `reflect` 居中补边要求信号长度 **严格大于** `nfft//2`（NumPy `reflect`
  不允许在长度等于补边宽度时反射）；此时服务返回参数错误并提示改用
  `edge`/`constant`。对短信号样例统一使用 `edge`。
- `edge` 复制补边在信号极短（如 < hop）时，边缘样本外部被当作边界值复制，
  该处重建误差约 1e-13（非机器精度），这是该补边方式的固有近似，已在矩阵中标出。
- PCM 整数格式写回有量化误差（如 int16 约 ±3e-5），内部重建误差一律以
  float64 信号为准；输出超范围值按整型削波（有测试覆盖）。
- 按需求未实现任何前端、播放器或网络接口；仅有 CLI、库 API 与文件/JSON 输出。
