"""Command-line interface for the ScL toolchain.

    python -m sclang.cli run     FILE         run converted IR (via VM)
    python -m sclang.cli ref     FILE         run source reference interpreter
    python -m sclang.cli check   FILE         lex+parse+analyze; report info
    python -m sclang.cli compile FILE         print closure-converted IR
    python -m sclang.cli tokens  FILE         print the token stream
    python -m sclang.cli diff    FILE         run both, diff their output

With ``-`` as FILE the source is read from stdin. Add ``--json`` for
machine-readable output (except compile/check text views).
"""

import argparse
import json
import sys

from .errors import SclError
from .ir import disassemble
from .pipeline import (analyze, analysis_json, error_payload,
                       run_frontend, run_reference, tokens_json)


def _read(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="sclang",
                                 description="ScL scope/closure toolchain")
    ap.add_argument("command",
                    choices=["run", "ref", "check", "compile", "tokens",
                             "diff"])
    ap.add_argument("file")
    ap.add_argument("--json", action="store_true",
                    help="emit JSON (run/ref/check/tokens)")
    args = ap.parse_args(argv)

    source = _read(args.file)
    try:
        if args.command == "tokens":
            from .lexer import Lexer
            tokens = Lexer(source).tokenize()
            if args.json:
                print(json.dumps(tokens_json(tokens), ensure_ascii=False,
                                 indent=2))
            else:
                for t in tokens:
                    print(f"{str(t.span):>12}  {t.kind.name:<10} {t.value!r}")
            return 0

        fe = analyze(source)

        if args.command == "check":
            if args.json:
                print(json.dumps({"ok": True, **analysis_json(fe)},
                                 ensure_ascii=False, indent=2))
            else:
                print(f"functions: {len(fe.resolution.functions)}")
                for fi in fe.resolution.functions:
                    frees = ", ".join(
                        f"{b.name}{'*' if b.boxed else ''}" for b in fi.free)
                    print(f"  [{fi.depth}] {fi.name}: slots={fi.slots} "
                          f"free=[{frees}]")
                boxed = [b.name for b in fe.resolution.bindings if b.boxed]
                print("boxed bindings:", ", ".join(boxed) if boxed else "(none)")
            return 0

        if args.command == "compile":
            print(disassemble(fe.module))
            return 0

        if args.command == "run":
            result, out = run_frontend(fe)
            return _report(args.json, result, out)

        if args.command == "ref":
            result, out = run_reference(fe)
            return _report(args.json, result, out)

        if args.command == "diff":
            r1, o1 = run_reference(fe)
            r2, o2 = run_frontend(fe)
            match = o1 == o2
            if args.json:
                print(json.dumps({"reference_output": o1, "vm_output": o2,
                                  "outputs_match": match}, ensure_ascii=False,
                                 indent=2))
            else:
                print("reference:", o1)
                print("vm       :", o2)
                print("MATCH" if match else "MISMATCH")
            return 0 if match else 1

    except SclError as e:
        if args.json:
            print(json.dumps(error_payload(e), ensure_ascii=False, indent=2))
        else:
            print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


def _report(as_json, result, out):
    if as_json:
        print(json.dumps({"ok": True, "output": out, "result": _val(result)},
                         ensure_ascii=False, indent=2))
    else:
        for line in out:
            print(line)
    return 0


def _val(v):
    return v if v is None or isinstance(v, (str, int, bool)) else str(v)


if __name__ == "__main__":
    sys.exit(main())
