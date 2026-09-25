"""Command-line interface: JSON request in, JSON result out.

Usage:
    python -m dtmf_service.cli --request examples/request_sample.json
    python -m dtmf_service.cli --input audio.wav [--rate 8000]
    python -m dtmf_service.cli --synthesize "159#" [--snr 20] [--offset 1.0]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .service import detect_file, handle_request
from .synth import synthesize_sequence
from .service import detect_samples


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dtmf-service",
        description="Offline dual-tone (DTMF) detection — numeric/JSON output only.",
    )
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--request", type=Path, help="JSON request file")
    src.add_argument("--input", type=Path, help="local PCM/WAV file")
    src.add_argument("--synthesize", metavar="KEYS", help="synthesize a key sequence")
    parser.add_argument("--rate", type=int, default=8000, help="sample rate (Hz)")
    parser.add_argument("--snr", type=float, default=None, help="synth SNR (dB)")
    parser.add_argument("--offset", type=float, default=0.0, help="synth freq offset (%)")
    parser.add_argument("--tone-ms", type=float, default=100.0, help="synth tone duration")
    parser.add_argument("--gap-ms", type=float, default=50.0, help="synth gap duration")
    parser.add_argument("--output", type=Path, default=None, help="write JSON result here")
    args = parser.parse_args(argv)

    if args.request:
        request = json.loads(args.request.read_text(encoding="utf-8"))
        result = handle_request(request)
    elif args.input:
        result = detect_file(args.input, args.rate)
    else:
        samples = synthesize_sequence(
            args.synthesize,
            args.rate,
            tone_ms=args.tone_ms,
            gap_ms=args.gap_ms,
            snr_db=args.snr,
            freq_offset_pct=args.offset,
            seed=0,
        )
        result = detect_samples(samples, args.rate)

    text = json.dumps(result, indent=2, ensure_ascii=False)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
