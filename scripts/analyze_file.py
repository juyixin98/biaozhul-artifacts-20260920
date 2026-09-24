#!/usr/bin/env python3
"""Command-line demo: run the analyzer on a .tl file without starting HTTP.

Usage:
    python scripts/analyze_file.py examples/dangerous.tl
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.analysis import AnalysisRequest, run_analysis  # noqa: E402


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    code = Path(argv[1]).read_text(encoding="utf-8")
    report = run_analysis(AnalysisRequest(code=code))
    print(json.dumps(report, indent=2, ensure_ascii=False))
    print("-" * 72, file=sys.stderr)
    print(f"verdict: {report['verdict']}  "
          f"({report['finding_count']} finding(s), "
          f"{report['fixpoint_iterations']} fixpoint iterations)",
          file=sys.stderr)
    for f in report["findings"]:
        print(f"  {f['sink']} at {f['function']}:{f['line']} "
              f"[{f['certainty']}]", file=sys.stderr)
        for ep in f["evidence_paths"]:
            print(f"    path: {ep['rendered']}", file=sys.stderr)
    return 0 if report["verdict"] == "safe" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
