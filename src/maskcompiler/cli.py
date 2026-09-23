"""Command-line interface.

Examples
--------
Generate local test keys::

    maskcompiler keygen --out local-test-keys.json

Compile a ruleset (validation only)::

    maskcompiler compile -r rules.json

Apply a ruleset to a document::

    maskcompiler apply -r rules.json -d document.json --key-file local-test-keys.json

Run the local HTTP service::

    maskcompiler serve --key-file local-test-keys.json --port 8080
"""

import argparse
import json
import logging
import sys
from typing import List, Optional

from .compiler import compile_ruleset
from .engine import apply_ruleset
from .errors import MaskCompilerError
from .keys import generate_bundle, load_keyfile, save_keyfile
from .logsafe import install_safe_logging
from .service import serve

logger = logging.getLogger("maskcompiler.cli")


def _load_json(path: str):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _cmd_keygen(args: argparse.Namespace) -> int:
    save_keyfile(args.out)
    print("wrote %s (mode 0600)" % args.out)
    return 0


def _cmd_compile(args: argparse.Namespace) -> int:
    compiled = compile_ruleset(_load_json(args.rules))
    summary = [
        {"id": e.rule_id, "priority": e.priority}
        for e in sorted(compiled.entries, key=lambda e: e.rule_id)
    ]
    json.dump({"ok": True, "rules": summary}, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


def _cmd_apply(args: argparse.Namespace) -> int:
    compiled = compile_ruleset(_load_json(args.rules))
    document = _load_json(args.document)
    if args.key_file:
        bundle = load_keyfile(args.key_file)
    elif _needs_keys(compiled):
        # Allowed for local experimentation only; hash output is not stable
        # across runs without a key file.
        logger.warning("no --key-file; generated ephemeral keys (outputs are run-specific)")
        bundle = generate_bundle()
    else:
        bundle = None
    if bundle is not None:
        compiled.bind_keys(bundle)
    result = apply_ruleset(compiled, document)
    payload = {
        "output": result.output,
        "report": {
            "transformed_locations": result.report.transformed_locations,
            "matched_rules": result.report.matched_rules,
        },
    }
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


def _needs_keys(compiled) -> bool:
    names = {type(e.transform).__name__ for e in compiled.entries}
    return "HashTransform" in names or "EncryptTransform" in names


def _cmd_serve(args: argparse.Namespace) -> int:
    bundle = load_keyfile(args.key_file) if args.key_file else None
    serve(host=args.host, port=args.port, key_bundle=bundle)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="maskcompiler", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p_keygen = sub.add_parser("keygen", help="generate a local 0600 key file")
    p_keygen.add_argument("--out", required=True)
    p_keygen.set_defaults(func=_cmd_keygen)

    p_compile = sub.add_parser("compile", help="validate and summarize a ruleset")
    p_compile.add_argument("-r", "--rules", required=True)
    p_compile.set_defaults(func=_cmd_compile)

    p_apply = sub.add_parser("apply", help="apply a ruleset to a JSON document")
    p_apply.add_argument("-r", "--rules", required=True)
    p_apply.add_argument("-d", "--document", required=True)
    p_apply.add_argument("--key-file", default=None)
    p_apply.set_defaults(func=_cmd_apply)

    p_serve = sub.add_parser("serve", help="run the local HTTP service")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8080)
    p_serve.add_argument("--key-file", default=None)
    p_serve.set_defaults(func=_cmd_serve)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    install_safe_logging(logging.DEBUG if args.verbose else logging.INFO)
    try:
        return args.func(args)
    except MaskCompilerError as exc:
        # Exception messages are data-free by construction.
        logger.error("%s: %s", type(exc).__name__, exc)
        return 2
    except FileNotFoundError as exc:
        logger.error("file not found: %s", exc.filename)
        return 2
    except json.JSONDecodeError as exc:
        logger.error("invalid JSON input: %s", exc.msg)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
