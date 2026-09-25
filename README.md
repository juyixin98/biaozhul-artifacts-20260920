# PCM 容器校验（pcm-container）

纯后端离线信号处理服务：用 Python + NumPy 实现 WAV PCM 子集的严格读写、
容器校验、整数 PCM ↔ 浮点振幅转换，以及合成信号的生成与容器转换。

**没有播放器、没有 GUI、没有网络服务。** 输入是请求 JSON / 本地文件，
输出只有数值（stdout JSON 报告）和数据文件（`.wav` / `.raw` / `.npy`）。

---

## 1. 环境与安装

- Python 3.8+（开发与验证环境：Python 3.12.3，NumPy 2.5.3）
- 唯一运行依赖：NumPy

```bash
pip install -r requirements.txt
```

## 2. 支持的 WAV PCM 子集

| 项目 | 接受 | 拒绝 |
|---|---|---|
| 容器 | RIFF/WAVE | 非 RIFF、非 WAVE 标识 |
| 编码 | 无压缩整数 PCM（`wFormatTag=1`） | IEEE float(3)、ADPCM、其它压缩 |
| 位深 | **16 位、24 位**有符号小端 | 8 位、32 位整数、32 位 float 等 |
| 声道 | 任意 ≥1 声道（交错排列） | 0 声道 |
| chunk | 未知 chunk 一律按其 size 跳过 | 重复 fmt/data、缺 fmt/data |
| 补齐 | 奇数 chunk 的 pad 字节（内部必须存在；文件尾缺失时宽容接受） | 内部缺 pad 导致错位 |
| RIFF 长度 | 声明与实际不符不致命，在报告中标记 `riff_size_matches_file=false` | — |
| data 对齐 | `len(data) % (channels*bits/8) == 0` | 余数不为 0 → `PcmAlignmentError` |
| fmt 一致性 | block align = ch×b/8、byte rate = rate×block | 字段互相矛盾 |

## 3. 振幅转换约定

- 正满量程码 `2^(n-1)-1` → 振幅 `1 - 1/2^(n-1)`（满量程刻度 +1.0）；
  负满量程码 `-2^(n-1)` → 精确 **-1.0**（负方向多一个码）。
- 浮点 → 整数：`rint(x·2^(n-1))` 最近值舍入，再裁剪到码域；
  NaN 映射为 0，±Inf 映射为 ±满量程。
- 往返性质（测试覆盖）：整数 → float → 整数**逐码恒等**。

## 4. 使用方式

### CLI（离线批处理）

```bash
python -m pcm_container run examples/01_synthesize_sine.json
python -m pcm_container run req1.json req2.json req3.json   # 多请求 -> JSON 数组
cat req.json | python -m pcm_container run -               # stdin
```

退出码：`0` 成功；`2` 请求/容器被拒绝（`PcmError` 子类，错误 JSON 在 stderr）；
`1` 环境性错误（文件缺失、JSON 语法错误）。

### 请求动作

| action | 必填字段 | 说明 |
|---|---|---|
| `synthesize` | wave_type, sample_rate, duration | 合成信号并量化写出。wave_type：`sine`/`square`/`sawtooth`/`chirp`/`noise` |
| `inspect_wav` | input_wav | 容器校验 + 数值统计（min/max/mean/rms/peak），可选导出 npy/raw |
| `convert` | input_wav(或 input_raw), output_wav | 容器/位深转换（16↔24）；不做重采样（请求改采样率会被拒绝） |
| `wav_to_raw` | input_wav, output_raw | 抽出交错小端 PCM 裸数据 |
| `raw_to_wav` | input_raw, output_wav, sample_rate, channels, bits | 裸 PCM 加 RIFF 头；长度不对齐即拒绝 |

可选输出字段：`output_wav` / `output_raw` / `output_npy`。
`duration*sample_rate` 必须是整数帧，否则报对齐错误。

### 作为库

```python
from pcm_container import read_wav, write_wav
from pcm_container.amplitude import to_float, from_float

w = read_wav("in.wav")          # w.samples: int32, shape (n_frames, channels)
amps = to_float(w.samples, w.bits)
codes = from_float(amps, 16)    # 舍入 + 裁剪
write_wav("out.wav", codes, w.sample_rate, w.channels, 16)
```

## 5. 一键复现（无需真实录音）

```bash
# 单元测试
python -m unittest discover -s tests -v        # 59 个用例

# 端到端样例 + 边界文件生成 + 拒绝行为验证
bash examples/run_examples.sh
```

`examples/generate_edge_cases.py` 全部用合成字节构造：
截断 chunk、错误采样对齐、极值样本、未知 chunk、RIFF 长度篡改、8 位与 float 格式。

## 6. 目录结构

```
pcm_container/
  errors.py      PcmError 异常层级（格式/截断/对齐）
  amplitude.py   整数码 <-> float 振幅（16/24 位）
  signals.py     合成信号（正弦/方波/锯齿/扫频/确定性噪声）
  wavio.py       RIFF/WAVE 解析与写出（chunk 跳过、pad、RIFF 长度）
  service.py     请求动作执行（synthesize/inspect/convert/raw 往返）
  __main__.py    CLI: python -m pcm_container run ...
examples/        请求样例 JSON、边界文件生成器、端到端脚本
tests/           59 个自动化测试
tests_results.log  最近一次完整测试输出（2026-09-24 实测）
RUNLOG.md        实际运行命令与结果记录
```

## 7. 明确不做的事

- 不播放、不录制、不提供任何 UI/Web 服务；
- 不做重采样、抖动(dither)、DSP 滤波；
- 不支持 extensible WAVEFORMATEXTENSIBLE、BWF 元数据编辑（未知 chunk 可跳过/透传）。
