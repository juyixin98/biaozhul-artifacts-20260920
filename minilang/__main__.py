"""Command line: ``python -m minilang path/to/file.ml`` prints a JSON tree.

With no file argument, source is read from stdin.
"""
import json
import sys

from .document import full_parse
from .serialize import result_to_dict


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) > 1:
        print("usage: python -m minilang [FILE]", file=sys.stderr)
        return 2
    if argv:
        with open(argv[0], "r", encoding="utf-8") as f:
            text = f.read()
    else:
        text = sys.stdin.read()
    result = full_parse(text)
    print(json.dumps(result_to_dict(result), indent=2, ensure_ascii=False))
    return 1 if result.diagnostics else 0


if __name__ == "__main__":
    raise SystemExit(main())
