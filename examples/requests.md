# 请求样例（命令行 + Python API）

所有命令均在仓库根目录执行。命令行入口只产出数值和文件，无任何界面。

## 1. 环境变量

```bash
export PYTHONPATH=src
```

（或 `pip install -e .` 后直接使用 `sudden-anomaly` 可执行命令。）

## 2. 合成场景

```bash
# 全部三种：step / drift / spikes
python3 -m sudden_anomaly.cli --scenario all --out-dir out

# 单一场景
python3 -m sudden_anomaly.cli --scenario step --out-dir out
python3 -m sudden_anomaly.cli --scenario drift --threshold 5 --out-dir out
python3 -m sudden_anomaly.cli --scenario spikes --sample-rate 100 --out-dir out
```

## 3. 逐块（分包）处理 —— 与一次性处理逐点一致

```bash
python3 -m sudden_anomaly.cli --scenario all --block-size 137 --out-dir out_block
python3 -m sudden_anomaly.cli --scenario step --block-size 1   # 逐点到达
```

## 4. 缺样

```bash
# 合成信号中随机注入 5% NaN
python3 -m sudden_anomaly.cli --scenario spikes --missing-rate 0.05 --out-dir out/miss
```

## 5. 本地 PCM 文件

支持格式：`s16`、`s32`、`float32`、`float64`、`uint8`，可选大小端。

```bash
# 仓库自带的演示文件（s16 小端，2000 样本，第 1234 点有强尖峰）
python3 -m sudden_anomaly.cli \
    --pcm examples/data/demo_spike.s16 \
    --pcm-format s16 --pcm-endian little \
    --sample-rate 100 \
    --window-size 512 --prime-size 64 --recent-size 32 \
    --threshold 5.0 \
    --out-dir out/pcm

# 分块处理同一文件
python3 -m sudden_anomaly.cli --pcm examples/data/demo_spike.s16 \
    --pcm-format s16 --block-size 100 --out-dir out/pcm_block
```

## 6. 机器可读 JSON 到 stdout

```bash
python3 -m sudden_anomaly.cli --scenario all --out-dir out --json
```

## 7. Python API

### 7.1 一次性离线检测

```python
import numpy as np
from sudden_anomaly import DetectorConfig, detect_offline, evaluate
from sudden_anomaly import make_spikes

cfg = DetectorConfig(threshold=5.0, window_size=512, recent_size=32)
scenario = make_spikes(spike_indices=[400, 900, 1500])
result = detect_offline(scenario.signal, cfg)

print(result.anomaly_indices)          # [400 900 1500]
print(result.decisions[400])           # ANOMALY
report = evaluate(scenario, result)
print(report.detection_rate)           # 1.0
print(report.events[0].delay_samples)  # 0
```

### 7.2 逐块流式（状态在调用间保持）

```python
from sudden_anomaly import StreamingAnomalyDetector, DetectorConfig

det = StreamingAnomalyDetector(DetectorConfig())
for block in stream_of_blocks:          # 任意块大小、任意次数
    out = det.feed(block)               # out 与该块逐点对齐
    print(out.anomaly_indices)
```

### 7.3 逐点喂入

```python
det = StreamingAnomalyDetector(DetectorConfig())
for value in samples:
    r = det.feed([value])
    assert r.decisions[0] in ("WARMING", "NORMAL", "ANOMALY", "NOT_DECIDED")
```

### 7.4 缺样

```python
x[10] = np.nan            # NaN = 缺失；不插补、不更新统计量
r = detect_offline(x)
assert r.decisions[10] == "NOT_DECIDED"
```

### 7.5 读写文件

```python
from sudden_anomaly.pcm_io import read_pcm, write_point_csv, write_json_summary

x, info = read_pcm("examples/data/demo_spike.s16",
                   sample_format="s16", sample_rate=100.0)
result = detect_offline(x)
write_point_csv("points.csv", result, sample_rate=100.0, signal=x)
write_json_summary("summary.json", result=result)
```
