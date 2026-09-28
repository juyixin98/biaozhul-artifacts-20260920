"""复现实验 CLI：

    python -m app.reproducibility.cli --workdir ./data/repro --out report.json

退出码 0 仅当 summary.overall == PASS。
"""
from __future__ import annotations

import argparse
import json
import sys

from .runner import run_reproduction_experiment


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行分层区组随机化复现实验")
    parser.add_argument("--workdir", default="./data/repro",
                        help="数据库/种子/日志的工作目录")
    parser.add_argument("--out", default=None, help="JSON 报告输出路径")
    args = parser.parse_args(argv)

    report = run_reproduction_experiment(workdir=args.workdir)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"报告已写入 {args.out}")
    else:
        print(text)

    summary = report["summary"]
    print("\n==== 汇总 ====", file=sys.stderr)
    for key, value in summary.items():
        print(f"  {key}: {value}", file=sys.stderr)
    return 0 if summary["overall"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
