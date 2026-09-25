# 流式短时傅里叶（STFT）离线信号处理服务

纯后端 Python + NumPy 项目：输入合成信号或本地 PCM 文件，输出数值结果与文件。
**不含任何播放器、界面或前端。**

## 功能

- 整段 STFT / 逆 STFT（`stft_service.stft`）
- 分块（流式）STFT / 逆 STFT（`stft_service.stream`），与整段计算逐位一致
- 窗长、步长、补边方式全部显式可配
- 重建采用窗重叠权重归一（weighted overlap-add，wOLA）
- 不满足可重建条件时给出明确诊断（COLA / NOLA 检查 + 零权重位置列表）
- 合成信号生成（确定性，可复现）与原始 PCM 读写（s16 / s32 / f32 / f64）

## 数学约定

- 窗函数：周期（periodic）约定，长度 = `n_fft`。周期 Hann 在 50% 重叠下满足 COLA。
- 帧起点：`m * hop_length`（补边后的坐标）。
- `center=True`（默认）：信号两端各补 `n_fft // 2` 个样本，使第 `m` 帧中心对准原信号第
  `m * hop` 个样本；逆变换后按补边量裁剪回原长度。
  - `pad_mode="constant"`：补零（流式路径仅支持此模式，因果，无需未来样本）。
  - `pad_mode="reflect"`：镜像补边，要求 `len(x) + 1 >= n_fft`，否则显式报错。
- `center=False`：不补边，只产出完整落入信号的帧；尾部无法覆盖的样本不重建，
  并在诊断中以 `uncovered_tail_length` 报告。
- 分析与合成使用同一窗，重建为

  ```
  y[n] = Σ_m w[n - m·hop] · x_m[n - m·hop]  /  Σ_m w[n - m·hop]²
  ```

  分母（窗重叠权重）> 0 的位置即精确重建；为 0 的位置输出 0 并列入诊断。

## 可重建性诊断

`cola_diagnostic(config, length)` 报告：

- `cola_holds` / `cola_constant`：无限周期格点上 `Σ_m w` 是否为常数（经典 COLA）。
- `nola_holds` / `nola_zero_positions_periodic`：无限周期格点上 `Σ_m w²` 是否处处
  严格为正（本实现分析=合成窗时的实际可重建条件，NOLA）。
- `zero_weight_positions_all`：给定有限长度时实际缓冲区中的零权重位置
  （含补边边界——`center=True` 时这些位置会被裁掉，不算失败）。

`istft` 返回的 `info` 中：

- `reconstruction_possible`：输出信号区域是否全部权重 > 0。
- `zero_weight_positions`：零权重的输出样本位置（这些位置输出定义为 0）。
- `min_weight`：信号区域最小权重。

典型失败例：周期 Hann 且 `hop == n_fft`（无重叠），每帧窗端点 `w=0` 落在无其他帧
覆盖的位置 → `nola_holds=false`，诊断列出具体位置（见
`examples/request_diagnostic.json`）。

## 安装与运行

无第三方运行时依赖之外的安装步骤，仅需 Python ≥ 3.10 与 NumPy：

```bash
pip install numpy        # 唯一依赖
python3 -m unittest discover -s tests -v   # 运行测试
```

### 运行一个任务

```bash
python3 -m stft_service.service examples/request_synthetic.json
python3 -m stft_service.service examples/request_pcm.json
python3 -m stft_service.service examples/request_diagnostic.json   # 故意不可重建，看诊断
```

退出码：成功 0；参数/输入非法 2（stdout 打印 `{"status": "error", "error": ...}`）。

### 请求 JSON 字段

| 字段 | 说明 |
|---|---|
| `input.type` | `"synthetic"` 或 `"pcm"` |
| `input.length / sample_rate / frequencies / amplitudes / noise_std / seed` | 合成信号参数（确定性） |
| `input.path / input.sample_format` | PCM 文件路径与格式（`s16`/`s32`/`f32`/`f64`，小端、单声道、无文件头） |
| `stft.n_fft` | 窗长 = FFT 长度 |
| `stft.hop_length` | 步长（要求 `1 <= hop <= n_fft`） |
| `stft.window` | `hann` / `hamming` / `blackman` / `rect` |
| `stft.center` | 是否两端补 `n_fft//2`（默认 true） |
| `stft.pad_mode` | `constant`（默认）或 `reflect` |
| `chunk_size` | 流式分块大小，整数或整数列表（循环使用），如 `[100, 200, 37]` |
| `output_dir` | 输出目录（相对请求文件所在目录） |
| `output_format` | 额外重建 PCM 的整型/浮点格式（默认 `f64`） |
| `write_csv` / `csv_rows` | 是否输出频谱预览 CSV 及行数 |

### 输出文件（写入 `output_dir`）

- `spectrum_whole.npy` / `spectrum_stream.npy`：复数频谱 `(n_frames, n_fft//2+1)`，标准 `.npy`
- `magnitude_whole.npy`：幅度谱
- `reconstructed_whole.pcm` / `reconstructed_stream.pcm`：重建信号（float64 原始 PCM）
- `original.pcm`：输入信号副本（float64 原始 PCM）
- `reconstructed_whole.<fmt>.pcm`：当 `output_format != f64` 时的整型量化重建
- `spectrum_preview.csv`：前若干帧 + 末两帧的 real/imag/magnitude（可选）
- `report.json`：完整报告（帧数对比、COLA/NOLA 诊断、零权重位置、RMSE / 最大绝对误差 /
  相对误差能量 / SNR、整段 vs 分块最大偏差）

## 项目结构

```
stft_service/
  __init__.py     包导出
  windows.py      周期窗构造（hann/hamming/blackman/rect）
  stft.py         配置校验、补边、整段 STFT/iSTFT、COLA/NOLA 诊断
  stream.py       StreamingSTFT / StreamingISTFT（与整段逐位一致）
  signals.py      确定性合成信号
  pcm_io.py       原始 PCM 读写
  service.py      任务执行 + CLI（python3 -m stft_service.service）
tests/            unittest 测试（65 个用例）
examples/         请求样例 + 运行产物
RUNLOG.md         实际运行的命令与结果记录
```

## 测试覆盖（要点）

- 整段 ↔ 分块前向逐位一致：多种分块大小（1、7、64、128、500、混合 `[37,100,3]`）、
  大于信号的块、小于 hop 的块
- 短输入：长度 0、1、2–7、小于窗长（50 vs n_fft=256）
- 尾块：长度覆盖 hop 的全部剩余类（`480 + 0..31`，hop=32）
- 零窗权重位置：Hann + `hop == n_fft`（整段与流式都标记同样的零权重位置，
  其余位置仍精确重建）；`center=False` 时周期 Hann 第 0 样本的本征零权重
- 逆变换分块：按 1/2/5/100 帧分组推送，与整段一致
- PCM 往返：s16/s32/f32/f64 精度界、裁剪
- 诊断：COLA 常数、NOLA 成立/不成立、reflect 补边过短报错、非法参数报错

## 限制（如实说明）

- 流式前向仅支持 `pad_mode="constant"`（reflect 需要整段信号，属非因果补边）。
- 分块按“帧”对齐处理；`center=False` 时信号尾部不足一帧的部分不重建（诊断中报告）。
- PCM 为无文件头原始格式，不包含 WAV/RIFF 容器解析。
- 仅单声道、float64 内部计算。
