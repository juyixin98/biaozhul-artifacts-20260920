# fir_stream — 流式 FIR 状态管理(纯后端)

离线信号处理服务:多通道 FIR 分块卷积、运行中滤波器切换(明确交叉渐变,
无突跳)、每通道状态隔离。输入为合成信号或本地 PCM 文件,输出仅为数值
报告(JSON)与 PCM 文件。**不包含任何播放器或界面。**

## 环境

- Python ≥ 3.10(开发环境为 3.12.3)
- NumPy ≥ 1.24(开发环境为 2.5.3),唯一无其他第三方依赖
- 测试使用标准库 `unittest`,无需 pytest

## 目录结构

```
src/fir_stream/
  core.py           StreamingFIR:单通道流式 FIR(延迟线状态)
  engine.py         StreamingFIREngine:多通道引擎,交叉渐变切换
  reference.py      独立参考实现(整段卷积 + 逐样本分层模拟器)
  discontinuity.py  块边界突跳检测
  signals.py        合成测试信号(sine/noise/mixed/impulse/step/silence)
  filters.py        FIR 设计(lowpass/highpass/identity/delay,窗函数法)
  pcm.py            raw PCM 读写(s16le/s24le/s32le/f32le/f64le,帧交错)
  service.py        JSON 请求驱动的离线处理服务
  cli.py            命令行入口
tests/              53 个自动化测试(unittest)
examples/
  requests/         请求样例(switch_demo.json, pcm_reference.json)
  data/             示例 PCM 输入(由 CLI 生成)
```

## 快速开始

```bash
# 运行全部测试(无需安装,tests/_bootstrap.py 会把 src/ 加入路径)
python3 -m unittest discover -s tests -v

# 生成示例 PCM 输入
PYTHONPATH=src python3 -m fir_stream.cli make-example-pcm \
    examples/data/input_2ch_s16.pcm --seconds 2 --channels 2 --dtype s16le

# 执行处理请求(输出 PCM + report.json 到请求的 output.dir)
PYTHONPATH=src python3 -m fir_stream.cli run examples/requests/switch_demo.json
PYTHONPATH=src python3 -m fir_stream.cli run examples/requests/pcm_reference.json
```

也可以 `pip install .` 后直接使用 `python3 -m fir_stream.cli ...`。

## 核心设计

### 状态模型

- 每通道持有:一份**输入历史**(长度上限 `history_capacity`,默认 8192)
  和一个**滤波层列表**。通道之间无任何共享状态。
- 每个滤波层 = 一个 `StreamingFIR`(携带长度 M-1 的延迟线)+ 一个分段
  线性增益包络。

### 运行中切换与交叉渐变

`engine.switch(coeffs, channels=None, fade_len=L)` 在块边界处生效:

1. 用该通道的**输入历史 prime 新层的延迟线** —— 新滤波器从切换点起
   就拥有正确的内部状态,无预热瞬态;
2. 所有现存层(包括尚未淡入完成的层)目标增益置 0,新层从 0 淡入到 1,
   均在 L 个样本内**线性**完成。第 j 个渐变样本(0 基)上,旧层总增益
   为 `g_old·(1 − (j+1)/L′)`,新层为 `(j+1)/L`,**任意时刻所有层增益之和
   恒为 1**,因此切换点的一阶差分不会异常,不产生突跳;
3. 渐变结束且增益为 0 的层被移除,层数自然收敛回 1。

**切换中再切换**:对当前所有层再次执行"全部淡出 + 新层淡入",增益和
不变量在任意嵌套深度下都成立(有测试覆盖连续 3 次未完成渐变中的再切换)。

`fade_len=0` 为硬切换(立即替换层,不保证无突跳)。

### 空块与通道数变化

- **空块是严格无操作**:不推进包络、不更新历史、不创建通道,返回同形状
  空数组。
- 输入块通道数可逐块变化:新通道以零历史即时创建;消失的通道状态被
  **冻结保留**,再次出现时从中断处继续。

### 尾部(drain)

`engine.drain()` 以零输入冲刷所有通道,返回 `(max_order, C)` 的尾部。
`process` 输出与 `drain` 输出拼接后,与 `np.convolve(x, h, 'full')`
逐样本一致(验收点 1)。

## 请求 JSON 格式

```json
{
  "sample_rate": 48000,
  "input":  {"kind": "synthetic", "signal": {"kind": "mixed", "n": 9600,
             "channels": 2, "seed": 1}}
           | {"kind": "pcm", "path": "in.pcm", "channels": 2, "dtype": "s16le"},
  "filter":  {"kind": "lowpass", "cutoff": 1000, "num_taps": 65},
  "chunk_size": 512,
  "schedule": [{"at": 3000,
                "filter": {"kind": "highpass", "cutoff": 300, "num_taps": 65},
                "fade_len": 256, "channels": [0]}],
  "history_capacity": 8192,
  "output": {"dir": "out/demo", "pcm_dtype": "f32le", "write_input_pcm": true},
  "check":  {"threshold": null}
}
```

- `filter` / `schedule[].filter` 支持 `lowpass`、`highpass`、`identity`、
  `delay`、`coeffs`(直接给系数),见 `filters.design`。
- `schedule[].at` 为样本位置;服务层会把事件位置**强制对齐为块边界**
  (必要时截短当前块),切换在块之间生效。`channels` 省略表示所有通道。
- `check.threshold` 为突跳检测阈值;`null` 表示自动
  (`8 × 全局|diff|中位数`,下限 1e-9)。

输出:`<dir>/output.pcm`(长度 = 输入 + 滤波器尾部)、`<dir>/report.json`
(全部数值指标:块边界列表、参考比对误差、突跳检测报告、RMS/峰值等)。

## 验收标准与测试映射

| 验收点 | 测试 |
|---|---|
| 整段卷积参考比对(含尾部,与 `np.convolve(..., 'full')` 一致) | `test_01`, `test_02` |
| 不同分块大小输出不变(1/2/7/128/512/整块/不规则) | `test_03` |
| 运行中切换 + 交叉渐变无突跳(与独立逐样本参考比对) | `test_04` |
| 切换中再切换(连续 3 次未完成渐变,增益和恒为 1) | `test_05` |
| 通道隔离、通道数增减、冻结通道恢复 | `test_06` |
| 空块不改变状态(包络不推进、不建通道) | `test_06` |
| 边界不连续检测(含人工注入阶跃的阳性对照) | `test_07` |
| PCM 读写往返、服务端到端 | `test_07`, `test_08` |

参考实现独立性:`reference.py` 中的 `LayeredReferenceFIR` 是与引擎
**分开编写的逐样本模拟器**(不复用引擎代码),用于交叉渐变场景的逐样本
比对;无切换场景直接比对 `np.convolve`。

## 实测记录(2026-09-24,Python 3.12.3 + NumPy 2.5.3)

```
$ python3 -m unittest discover -s tests
.....................................................
----------------------------------------------------------------------
Ran 53 tests in 1.161s
OK
```

```
$ PYTHONPATH=src python3 -m fir_stream.cli run examples/requests/switch_demo.json
n_input_samples=9600, n_blocks=22, n_switches=3
reference_max_abs_err = 2.78e-16
discontinuity_ok = true   (边界最大 |diff| 0.117,与信号正常变化水平相当)
exit=0
```

```
$ PYTHONPATH=src python3 -m fir_stream.cli run examples/requests/pcm_reference.json
n_input_samples=96000, n_output_samples=96064 (含 64 样本尾部), n_blocks=188
reference_max_abs_err = 2.22e-16
discontinuity_ok = true (187 个块边界)
exit=0
```

**未通过项:无(53/53 通过)。**

开发过程中实际发现并修复的问题(均有对应回归测试):

1. `StreamingFIR` 延迟线存储顺序与拼接顺序不一致,导致尾部(drain)
   输出错误 —— 改为统一按时间顺序存储;
2. `_ChannelState.__slots__` 漏声明 `_capacity`,首次处理即 AttributeError;
3. 突跳自动阈值原用 99.9 分位数,会被跳变本身抬高而漏检 —— 改为对
   异常值鲁棒的中位数估计;
4. `write_pcm` 不创建父目录。

## 限制

- 直接时域卷积,未做 FFT 分块加速;超长滤波器(数万阶)离线场景可用
  但较慢,`history_capacity` 需 ≥ 最大滤波器阶数。
- 单次请求内输入通道数固定(引擎 API 本身支持逐块变化,见 `test_06`)。
- PCM 为 raw 无头格式,采样率/通道数/位宽需在请求中显式给出。
