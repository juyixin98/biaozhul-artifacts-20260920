#!/usr/bin/env python3
"""Build a JSON request body for POST /analyze from a .rf source file.

Usage:
    python3 samples/build_request.py examples/02_partial_init.rf > req.json
    python3 samples/build_request.py examples/04_loop_acquire.rf --loop-bound 3 > req.json
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("source_file")
    ap.add_argument("--loop-bound", type=int, default=2)
    ap.add_argument("--max-steps", type=int, default=10000)
    args = ap.parse_args()
    with open(args.source_file, "r", encoding="utf-8") as fh:
        source = fh.read()
    payload = {
        "filename": args.source_file,
        "source": source,
        "loop_bound": args.loop_bound,
        "max_steps": args.max_steps,
    }
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
