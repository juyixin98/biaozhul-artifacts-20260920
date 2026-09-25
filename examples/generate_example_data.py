#!/usr/bin/env python3
"""生成 examples/ 请求所引用的本地 PCM/WAV 样例文件。

文件写到 examples/data/ 下：
  * pair_s16_stereo_8k.pcm —— 16 位小端、交错双声道裸 PCM，chan 相对 ref 右移 21 样本；
  * pair_s16_stereo_8k.wav —— 同样内容的 16 位 PCM WAV（便于直接对照）。

用法（仓库根目录）::

    python examples/generate_example_data.py
"""

from pathlib import Path

import numpy as np

from delay_correlator.signals import (
    SyntheticSpec,
    make_delayed_pair,
    save_raw_pcm,
    write_wav16,
)


def main() -> None:
    here = Path(__file__).resolve().parent
    data_dir = here / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    spec = SyntheticSpec(
        kind="noise",
        duration=1.0,
        sample_rate=8000,
        delay_samples=21,
        noise_db=-25.0,
        amplitude=0.4,  # RMS 0.4 留足量化余量，峰值一般不削顶
        seed=20260924,
    )
    ref, chan = make_delayed_pair(spec)
    # 双保险限幅，确保不被 16 位编码削顶。
    ref, chan = np.clip(ref, -1.0, 1.0), np.clip(chan, -1.0, 1.0)

    pcm_path = data_dir / "pair_s16_stereo_8k.pcm"
    wav_path = data_dir / "pair_s16_stereo_8k.wav"
    save_raw_pcm(str(pcm_path), ref, chan, dtype="s16", interleaved=True)
    write_wav16(str(wav_path), [ref, chan], spec.sample_rate)

    print(f"已生成 {pcm_path}  ({pcm_path.stat().st_size} 字节)")
    print(f"已生成 {wav_path}  ({wav_path.stat().st_size} 字节)")
    print(f"内容: 8kHz 16-bit 双声道，chan 相对 ref 右移 +21 样本（chan 滞后），噪声 -25 dB")


if __name__ == "__main__":
    main()
