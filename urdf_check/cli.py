"""命令行入口:python -m urdf_check.cli <file.urdf> [--json] [--tolerance ...]

退出码: 0 = 通过(无 error);1 = 存在 error;2 = 输入不可解析/不安全。
"""

from __future__ import annotations

import argparse
import json
import sys

from .checks import CheckConfig
from .service import check_urdf


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="urdf-check",
        description="URDF 离线结构与惯性参数检查(纯后端,不控制硬件)",
    )
    p.add_argument("urdf", help="URDF 文件路径")
    p.add_argument("--json", action="store_true", help="以 JSON 输出报告")
    p.add_argument("--axis-norm-tol", type=float, default=CheckConfig.axis_norm_tol)
    p.add_argument("--psd-atol", type=float, default=CheckConfig.psd_atol)
    p.add_argument("--triangle-rtol", type=float, default=CheckConfig.triangle_rtol)
    return p


def main(argv=None) -> int:
    args = _build_argparser().parse_args(argv)
    try:
        with open(args.urdf, "rb") as fh:
            source = fh.read()
    except OSError as exc:
        print(f"无法读取文件: {exc}", file=sys.stderr)
        return 2

    cfg = CheckConfig(
        axis_norm_tol=args.axis_norm_tol,
        psd_atol=args.psd_atol,
        triangle_rtol=args.triangle_rtol,
    )
    report = check_urdf(source, cfg)

    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(
            f"robot={report.robot_name!r} links={report.link_count} "
            f"joints={report.joint_count} "
            f"errors={len(report.errors)} warnings={len(report.warnings)}"
        )
        for d in report.diagnostics:
            loc = d.node or "-"
            if d.attribute:
                loc += f"@{d.attribute}"
            line = f"L{d.line}" if d.line else "L?"
            print(f"[{d.severity:7s}] {d.code:32s} {line:>6s} {loc}")
            print(f"          {d.message}")
        print("结果: " + ("通过" if report.ok else "未通过"))

    has_error = bool(report.errors)
    if has_error and any(
        d.code in ("DTD_FORBIDDEN", "ENTITY_FORBIDDEN", "XACRO_FORBIDDEN",
                   "XML_SYNTAX_ERROR")
        for d in report.errors
    ):
        return 2
    return 1 if has_error else 0


if __name__ == "__main__":
    sys.exit(main())
