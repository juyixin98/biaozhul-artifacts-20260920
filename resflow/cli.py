"""Command line interface: analyze a source file and print a report or JSON."""
from __future__ import annotations

import json
import sys
from argparse import ArgumentParser, Namespace
from typing import List, Optional

from .analyzer import (
    DOUBLE_RELEASE,
    LEAK,
    RELEASE_NOT_HELD,
    USE_AFTER_RELEASE,
    USE_NOT_HELD,
    OVERWRITE_HELD,
    analyze_source,
)
from .errors import ResflowError

ERROR_CODES = {DOUBLE_RELEASE, USE_AFTER_RELEASE, LEAK, OVERWRITE_HELD}

SEVERITY = {
    DOUBLE_RELEASE: "error",
    USE_AFTER_RELEASE: "error",
    LEAK: "error",
    OVERWRITE_HELD: "error",
    RELEASE_NOT_HELD: "warning",
    USE_NOT_HELD: "warning",
}


def build_parser() -> ArgumentParser:
    parser = ArgumentParser(
        prog="resflow",
        description="Resource-release path analyzer for the resflow language.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_check = sub.add_parser("check", help="analyze a .rf source file")
    p_check.add_argument("file", help="resflow source file")
    p_check.add_argument("--format", choices=("text", "json"), default="text")
    p_check.add_argument("--loop-bound", type=int, default=2, help="max back edges per loop (default 2)")
    p_check.add_argument("--max-steps", type=int, default=10000)
    p_check.add_argument("--cfg", action="store_true", help="include the CFG in JSON output")

    p_server = sub.add_parser("serve", help="run the JSON HTTP service")
    p_server.add_argument("--host", default="127.0.0.1")
    p_server.add_argument("--port", type=int, default=8080)
    p_server.add_argument("--quiet", action="store_true")
    return parser


def render_text(result: dict) -> str:
    lines: List[str] = []
    lines.append("resflow analysis report")
    lines.append(f"file: {result['filename']}  (loop_bound={result['options']['loop_bound']})")
    counts = result["counts"]
    lines.append(
        f"functions: {counts['functions']}  paths: {counts['paths']}  "
        f"findings: {counts['findings']}  truncated: {counts['truncated_paths']}"
    )
    lines.append("")

    findings = result["findings"]
    if findings:
        lines.append("Findings:")
        for f in findings:
            sev = SEVERITY.get(f["code"], "note")
            loc = f["span"]
            lines.append(
                f"  #{f['id']} [{sev}] {f['code']} in {f['function']} "
                f"({loc['filename']}:{loc['start']['line']}:{loc['start']['column']}) "
                f"{f['message']}"
            )
    else:
        lines.append("Findings: none")
    lines.append("")

    for fn in result["functions"]:
        lines.append(f"function {fn['name']}{' throws' if fn['throws'] else ''}: "
                     f"{len(fn['paths'])} path(s)")
        for path in fn["paths"]:
            finding_note = (
                " findings=" + ",".join(str(i) for i in path["finding_ids"])
                if path["finding_ids"] else ""
            )
            lines.append(f"  path #{path['path_id']} [{path['kind']}]{finding_note}")
            for step in path["steps"]:
                edge = step["via_edge"]
                via = f"  via:{edge['kind']}:{edge['label']}" if edge else ""
                state = " ".join(f"{k}={v}" for k, v in sorted(step["state_after"].items()))
                loc = step["span"]
                lines.append(
                    f"    {step['node_id']:12s} {step['kind']:11s} "
                    f"L{loc['start']['line']:<4d} {step['label']}  [{state}]{via}"
                )
                for ev in step["events"]:
                    if ev["type"] == "finding":
                        lines.append(f"        -> finding #{ev['finding_id']} {ev['code']}: {ev['message']}")
                    else:
                        lines.append(f"        -> event {ev['type']}: " + ", ".join(
                            f"{k}={v}" for k, v in ev.items() if k != "type"))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def run_check(args: Namespace) -> int:
    try:
        with open(args.file, "r", encoding="utf-8") as fh:
            source = fh.read()
    except OSError as exc:
        print(f"error: cannot read {args.file}: {exc}", file=sys.stderr)
        return 2
    try:
        result = analyze_source(
            source,
            filename=args.file,
            loop_bound=args.loop_bound,
            max_steps=args.max_steps,
        )
    except ResflowError as exc:
        print(exc.format(), file=sys.stderr)
        return 2

    if args.format == "json":
        if not args.cfg:
            result = {**result, "functions": [
                {k: v for k, v in fn.items() if k != "cfg"} for fn in result["functions"]
            ]}
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        sys.stdout.write(render_text(result))

    error_findings = [f for f in result["findings"] if f["code"] in ERROR_CODES]
    return 1 if error_findings else 0


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "serve":
        from .server import run as run_server
        return run_server(["--host", args.host, "--port", str(args.port)] + (["--quiet"] if args.quiet else []))
    try:
        rc = run_check(args)
        sys.stdout.flush()
    except BrokenPipeError:
        # Downstream pipe (e.g. head) closed early; that is not an analysis error.
        try:
            sys.stdout.close()
        except BrokenPipeError:
            pass
        return 0
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
