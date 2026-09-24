# pcmval — PCM 容器校验离线服务

纯后端 Python + NumPy 项目:离线校验 WAV(RIFF/WAVE)PCM 容器、在整数 PCM
样本与浮点振幅之间转换、合成测试信号并做字节/振幅往返验证。**只返回数值
和文件,不含播放器、不含任何界面。**

## 支持的格式子集

| 项 | 支持 | 拒绝 |
|---|---|---|
| 容器 | RIFF/WAVE | RF64、AIFF、其他 |
| 编码 | PCM (0x0001);EXTENSIBLE (0xFFFE) 且子格式为 PCM GUID | IEEE float、A-law/μ-law 等 |
| 位深 | 16 bit、24 bit 整数 | 8/20/32/64 bit 等 |
| 声道 | 任意 ≥1(交错存储) | 0 |

容器规则(读端):

- **未知 chunk 跳过**:按声明长度跳过(含奇数长度的 1 字节 pad),并记录到
  `chunks` 目录;声明长度越过文件末尾视为**截断**,报错而不是静默跳过。
- **奇数字节补齐**:负载长度为奇数时后跟 1 字节 pad;pad 计入 RIFF 长度、
  不计入 chunk 长度。写端同样遵守。
- **RIFF 长度**:头声明总长度与文件实际长度不一致 → `RIFF_SIZE_MISMATCH`
  告警;末尾不足一个 chunk 头的残余字节 → `TRAILING_BYTES` 告警。
- **采样对齐**:`data` 字节数不是 `block_align` 整数倍 → 丢弃末尾残缺字节,
  保留完整帧,记 `PARTIAL_FRAME` 告警;`block_align` 与声道/位深不符 → 拒绝。
- 缺 `fmt `/`data` chunk、chunk 负载截断 → `WavFormatError`。

振幅约定(全项目统一):

- 浮点域 `[-1.0, 1.0)`;`float = int / 2^(bits-1)`,即 int16 满幅为
  `-32768 → -1.0`、`+32767 → 32767/32768`;24 bit 同理(`±8388608/8388607`)。
- 量化 `round(float * 2^(bits-1))`,越界裁剪(`+1.0 → +32767`,绝不翻转),
  NaN/Inf 拒绝。
- 24 bit 样本为小端 3 字节,解码做符号扩展,内存中用 int32 承载。

## 安装与运行

依赖只有 NumPy(Python ≥ 3.10,开发环境为 3.12.3 + NumPy 2.5.3):

```bash
pip install -r requirements.txt
```

### CLI

```bash
# 校验 WAV(容器结构 + 样本统计,JSON 输出)
python3 -m pcmval.cli validate input.wav [--strict]

# 校验无容器 raw PCM(需自报参数)
python3 -m pcmval.cli validate input.pcm --raw --bits 24 --channels 2 --sample-rate 48000

# 合成信号并写 WAV / raw PCM
python3 -m pcmval.cli synth --kind fullscale --bits 24 --sample-rate 8000 \
    --duration 0.002 -o out.wav --raw-out out.pcm

# 字节/振幅往返验证(合成源或已有 WAV)
python3 -m pcmval.cli roundtrip --kind sine --bits 16 -o rt.wav
python3 -m pcmval.cli roundtrip --path existing.wav

# JSON 请求(单个对象或数组;'-' 从 stdin 读)
python3 -m pcmval.cli request examples/requests/roundtrip_sine16.json
cat req.json | python3 -m pcmval.cli request -
```

退出码:`0` 成功;`1` 批量请求中部分条目失败(逐条给出 `ok`/`error`);
`2` 单条请求失败(参数错误、容器非法、文件不存在)。

### 请求格式

三个动作,字段见 `examples/requests/`:

- `validate`:`{action, path, strict?}`;raw PCM 加 `container:"raw", bits, channels, sample_rate`
- `synthesize`:`{action, kind: sine|silence|fullscale|ramp, sample_rate, duration, bits, channels?, amplitude?, out?, raw_out?}`
- `roundtrip`:`{action, kind|path, sample_rate, bits, channels?, out?}` —
  返回 `checks.payload_bytes_identical`(PCM 负载逐字节一致)、
  `checks.integer_samples_identical`、`checks.amplitude_roundtrip_identical`、
  `checks.whole_file_rebuild_identical`(干净文件整文件重写一致)与
  `quantization.max_abs_error_amplitude`(≤ 0.5 LSB)。

### Python API

```python
from pcmval import service, wavio, pcm, synth
result = wavio.read_wav("in.wav")          # 样本 + chunk 目录 + issues
blob   = wavio.encode_wav(samples, 48000, channels=2, bits=24)
out    = service.handle({"action": "roundtrip", "kind": "fullscale", "bits": 16})
```

## 测试与示例

```bash
python3 -m unittest discover -s tests -t . -v   # 66 个用例
bash examples/run_examples.sh                    # 端到端示例,输出到 examples/output/
```

- `tests/test_acceptance.py` — 验收场景:截断 chunk、错误采样对齐、极值样本、
  往返字节、格式拒绝,全部用合成字节构造,无需真实录音。
- `examples/make_fixtures.py` — 生成"问题容器"样本(截断 JUNK、奇 pad、
  非帧对齐 data、8bit、float32、RIFF 长度不符)。
- `examples/EXAMPLE_OUTPUT.log` — 示例脚本的完整真实输出。
- `RUNLOG.md` — 验证命令与结果记录(含通过/失败项说明)。

## 项目结构

```
pcmval/
  pcm.py      整数 PCM <-> 浮点振幅(16/24 bit,符号扩展、裁剪)
  wavio.py    WAV 读写与容器校验(chunk 目录、pad、RIFF 长度、截断检测)
  synth.py    合成信号(sine/silence/fullscale/ramp)
  service.py  请求处理(validate/synthesize/roundtrip)
  cli.py      命令行入口
tests/        unittest 套件(66 用例)
examples/     请求样例、fixture 生成器、运行脚本与真实输出
```
