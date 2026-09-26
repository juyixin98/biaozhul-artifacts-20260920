"""命令行入口:读取 JSON 请求文件,执行占据栅格融合,输出 JSON 结果。"""

from __future__ import annotations

import argparse
import json
import sys

from occupancy_grid.io import run_request


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="离散占据栅格融合(log-odds,离线 JSON 入口)"
    )
    parser.add_argument("--request", required=True, help="输入请求 JSON 文件路径")
    parser.add_argument(
        "--output",
        default=None,
        help="输出结果 JSON 文件路径(缺省打印到标准输出)",
    )
    args = parser.parse_args(argv)

    with open(args.request, "r", encoding="utf-8") as f:
        request = json.load(f)

    result = run_request(request)

    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        stats = result["scan_stats"]
        total_hits = sum(s["hit_count"] for s in stats)
        total_miss = sum(s["miss_count"] for s in stats)
        print(
            f"融合完成:{len(stats)} 帧扫描,{total_hits} 条命中射线,"
            f"{total_miss} 条未命中射线,结果已写入 {args.output}"
        )
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
