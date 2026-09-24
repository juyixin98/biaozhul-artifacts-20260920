# 运行记录（RUNLOG）

记录开发验收阶段实际执行的命令与结果。环境：

- OS：Linux 6.8.0-90-generic（x86_64）
- Python 3.12.3，NumPy 2.5.3，pytest 9.1.1
- 工作目录：`/home/admin/Downloads/biaozhul/opp134/a`

## 1. 自动化测试

命令：

```bash
python3 -m pytest tests/ -v
```

结果：**76 passed in 1.23s**（全部通过，无跳过、无失败）。

覆盖清单（按文件）：

- `test_correlator.py`（15 项）：FFT 互相关与直接法对拍（误差 ≤1e-10）、
  零滞后自相关=能量、**正/负延迟符号约定**、通道交换取负、相关系数界
  `[-1,1]`、自相关=1、反相关=-1、ZNCC 抗直流、常数信号无 NaN、
  max_lag 裁剪、重叠长度、非法参数拒绝。
- `test_estimate.py`（24 项）：正/负/零整数延迟（0,1,3,17,99 与
  -1,-5,-17,-100）、抛物线分数延迟（12.4 → 12.293）、静音/低能量/
  不相关噪声判不确定、SNR=0dB 仍可定位、周期正弦判 ambiguous、
  扫频无模糊、边界峰标记、峰查找工具（含平顶/常数/边界）、JSON 序列化。
- `test_pcmio.py`（11 项）：u8/s16/s32/f32 原始 PCM 往返、交错顺序、
  声道数不整除报错、WAV 8/16/32-bit 往返、24-bit WAV 读取、非法 dtype。
- `test_service.py`（16 项）：合成正/负延迟端到端、ms→样本换算、
  分块一致/不一致聚合、paired/interleaved/WAV 三种 PCM 输入、
  静音与周期请求判不确定、npz/csv 产物、合成→落盘→重分析一致、
  CLI run 与 4 个 demo 子进程。
- `test_window.py`（10 项）：单窗口、短信号、整除、重叠 hop、
  尾部零头取舍、空信号、非法参数。

开发过程中曾出现并已修复的问题（如实记录）：

1. **FFT 互相关符号反了**：初版用 `FFT(a)·conj(FFT(b))`，导致
   `B[n]=A[n−d]` 的峰落在 `−d`。直接法对拍测试当场捕获，改为
   `conj(FFT(a))·FFT(b)` 后误差降至 1e-15 量级，符号测试全部通过。
2. **常数信号被误判为峰**：`local_maxima` 对全平数组返回索引 0，
   已改为全平返回空。
3. **窗口尾部判断笔误**：`end - start >= window_size` 恒真导致零头窗口
   被无条件保留，已改为与 `min_partial` 比例比较。
4. **`wave.open` 不接受 `Path`**：pcmio 中统一 `str(path)`。
5. **CLI 子进程测试找不到包**：测试内以 `PYTHONPATH=项目根` 运行。

## 2. 样例请求实测

命令（`examples/` 下 4 个请求，结果已落盘 `examples/result_*.json`）：

```bash
python3 -m delay_correlator run examples/request_synthetic.json -o examples/result_synthetic.json
python3 -m delay_correlator run examples/request_pcm.json       -o examples/result_pcm.json
python3 -m delay_correlator run examples/request_periodic.json  -o examples/result_periodic.json
python3 -m delay_correlator run examples/request_silence.json   -o examples/result_silence.json
```

| 请求 | 真值 | 估计 | 状态 | 说明 |
|---|---|---|---|---|
| synthetic（噪声对，SNR 25dB） | +137 | **+137**（4 窗口全一致） | ok | 平均相干 0.998 |
| pcm（`stereo_pair.s16.pcm`） | +137 | **+137**（7 个重叠窗口全一致） | ok | 16-bit 量化后仍精确 |
| periodic（256 Hz 正弦，−40） | −40 | −40（窗口内） | **uncertain** | `ambiguous_peaks`，峰=1.0，次峰比 0.9989 |
| silence（全零） | +5 | 不输出 | **uncertain** | `low_energy` 等 |

## 3. 内置 demo 实测

```bash
python3 -m delay_correlator demo --case positive-delay   # lag=+17, ok
python3 -m delay_correlator demo --case negative-delay   # lag=-17, ok（负号正确）
python3 -m delay_correlator demo --case silence          # uncertain（低能量）
python3 -m delay_correlator demo --case periodic          # uncertain（多峰）
```

## 4. 验收矩阵（脚本实测，2026-09-24）

| 场景 | 真值 | 估计 | 峰相干 | 状态 |
|---|---|---|---|---|
| 噪声对 +50，清洁 | +50 | +50（分数 +50.000） | 1.0000 | ok |
| 噪声对 −50，清洁 | −50 | −50（分数 −50.000） | 1.0000 | ok |
| 噪声对 +50，SNR=10dB | +50 | +50 | 0.9546 | ok |
| 噪声对 +50，SNR=0dB | +50 | +50 | 0.7129 | ok |
| 噪声对 0 延迟 | 0 | 0 | 1.0000 | ok |
| 正弦 200Hz +20（周期） | +20 | +20 | 1.0000 | **uncertain**（ambiguous_peaks） |
| 扫频 +50 | +50 | +50 | 1.0000 | ok |
| 静音 | +50 | 不输出 | 0.0000 | **uncertain**（low_energy） |
| 独立噪声 | 0 | 不采信 | 0.0338 | **uncertain**（low_coherence） |

附加验证：

- 分数延迟 12.4 样本 → 整数 12，抛物线插值 12.293（误差 0.107 样本）；
- B 通道加直流 +5：ZNCC 峰 1.0000 且 lag 正确，NCC 峰跌至 0.0823
  （印证 ZNCC 抗直流）；
- 16-bit PCM 量化往返后估计 lag=9 正确，峰 1.0000。

## 5. 未通过项

无。最终 `python3 -m pytest tests/ -v` 全量 76 项通过。
（第 1 节列出的 5 个问题均在开发过程中被测试捕获并修复，修复后未再出现。）
