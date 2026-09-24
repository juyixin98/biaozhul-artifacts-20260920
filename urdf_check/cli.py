"""命令行入口：python -m urdf_check <file.urdf> [--json] [--strict-warnings]。"""

from __future__ import annotations

import argparse
import json
import sys

from .checker import DEFAULT_TOL, inspect_urdf_file
from .issues import Severity


def _format_text(report, path: str) -> str:
    lines = [f"检查目标：{path}"]
    if not report.issues:
        lines.append("结果：通过（0 error, 0 warning）")
        return "\n".join(lines)

    for i in report.issues:
        loc = i.node or "(文档级)"
        if i.attribute:
            loc += f" @{i.attribute}"
        if i.line:
            loc += f" (line {i.line})"
        lines.append(f"[{i.severity.upper():7}] {i.code}: {i.message}")
        lines.append(f"           位置：{loc}")
    lines.append(
        f"结果：{'未通过' if report.errors else '通过（有告警）'} "
        f"({len(report.errors)} error, {len(report.warnings)} warning)"
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="urdf_check",
        description="URDF 离线结构与物理参数检查（不解析实体、不执行宏、不控制硬件）。",
    )
    ap.add_argument("urdf", help="URDF 文件路径")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    ap.add_argument(
        "--strict-warnings", action="store_true",
        help="存在 warning 时也以非零码退出（默认仅 error 导致退出码 1）",
    )
    ap.add_argument("--axis-tol", type=float, default=DEFAULT_TOL["axis_norm"],
                    help=f"轴归一化容差（默认 {DEFAULT_TOL['axis_norm']:g}）")
    args = ap.parse_args(argv)

    report = inspect_urdf_file(args.urdf, tol={"axis_norm": args.axis_tol})

    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(_format_text(report, args.urdf))

    if report.errors or (args.strict_warnings and report.warnings):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
