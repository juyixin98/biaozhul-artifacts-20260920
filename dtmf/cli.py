"""命令行入口：合成 / 解码 / stdio 服务。

用法：
  python -m dtmf.cli synth --keys 159# --out sample.pcm [--snr-db 20] [--freq-dev-pct 1.0]
  python -m dtmf.cli decode --file sample.pcm [--sample-rate 8000]
  python -m dtmf.cli service          # 等价于 python -m dtmf.service
"""

from __future__ import annotations

import argparse
import json

from .detector import DetectorConfig, DtmfDetector
from .pcmio import read_pcm, write_pcm
from .synth import SynthConfig, synthesize, to_int16


def main() -> None:
    parser = argparse.ArgumentParser(prog="dtmf", description="双音频率识别离线工具")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_syn = sub.add_parser("synth", help="合成双音序列并写出 PCM/WAV")
    p_syn.add_argument("--keys", required=True)
    p_syn.add_argument("--out", required=True)
    p_syn.add_argument("--sample-rate", type=int, default=8000)
    p_syn.add_argument("--tone-ms", type=float, default=60.0)
    p_syn.add_argument("--gap-ms", type=float, default=40.0)
    p_syn.add_argument("--amplitude", type=float, default=0.5)
    p_syn.add_argument("--twist-db", type=float, default=0.0)
    p_syn.add_argument("--freq-dev-pct", type=float, default=0.0)
    p_syn.add_argument("--snr-db", type=float, default=None)
    p_syn.add_argument("--seed", type=int, default=None)

    p_dec = sub.add_parser("decode", help="解码本地 PCM/WAV 文件，输出 JSON")
    p_dec.add_argument("--file", required=True)
    p_dec.add_argument("--sample-rate", type=int, default=None,
                       help="原始 PCM 必填；WAV 可省略")
    p_dec.add_argument("--min-tone-ms", type=float, default=40.0)
    p_dec.add_argument("--energy-threshold", type=float, default=500.0)
    p_dec.add_argument("--twist-db", type=float, default=8.0)

    sub.add_parser("service", help="stdio JSON 服务模式")

    args = parser.parse_args()

    if args.cmd == "synth":
        cfg = SynthConfig(sample_rate=args.sample_rate, tone_ms=args.tone_ms,
                          gap_ms=args.gap_ms, amplitude=args.amplitude,
                          twist_db=args.twist_db,
                          freq_deviation_pct=args.freq_dev_pct,
                          snr_db=args.snr_db, seed=args.seed)
        signal, fs = synthesize(args.keys, cfg)
        write_pcm(args.out, to_int16(signal), fs)
        print(json.dumps({"ok": True, "out_file": args.out,
                          "num_samples": int(len(signal)), "sample_rate": fs}))
    elif args.cmd == "decode":
        samples, fs = read_pcm(args.file, args.sample_rate)
        detector = DtmfDetector(DetectorConfig(
            sample_rate=fs, min_tone_ms=args.min_tone_ms,
            energy_threshold=args.energy_threshold, twist_db=args.twist_db))
        print(json.dumps(detector.decode(samples).to_dict(), ensure_ascii=False, indent=2))
    elif args.cmd == "service":
        from .service import main as service_main
        service_main()


if __name__ == "__main__":
    main()
