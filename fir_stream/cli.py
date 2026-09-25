"""Command-line entry point: run one offline FIR job from a JSON file.

Usage:
    python -m fir_stream.cli path/to/job.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from .service import run_job


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline streaming-FIR job runner")
    parser.add_argument("job", help="path to a job JSON file")
    args = parser.parse_args(argv)

    with open(args.job, "r", encoding="utf-8") as fh:
        job = json.load(fh)
    base_dir = os.path.dirname(os.path.abspath(args.job))
    report = run_job(job, base_dir=base_dir)
    json.dump(report, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
