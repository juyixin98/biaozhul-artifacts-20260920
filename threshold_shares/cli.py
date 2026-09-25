"""Command-line interface: local server, in-process split/recover, sealing.

Examples:
    python -m threshold_shares.cli serve --port 8080
    python -m threshold_shares.cli split --threshold 3 --total 5 --text "hello"
    echo -n "hello" | python -m threshold_shares.cli split -k 3 -n 5
    python -m threshold_shares.cli recover shares.json
    python -m threshold_shares.cli seal "SSS1\$..." --passphrase-file pw.txt
"""

import argparse
import base64
import json
import sys

from . import __version__, service
from .sealing import seal_share, unseal_share


def _read_secret(args) -> bytes:
    if args.text is not None:
        return args.text.encode("utf-8")
    if args.secret_file:
        with open(args.secret_file, "rb") as fh:
            return fh.read()
    if not sys.stdin.isatty():
        data = sys.stdin.buffer.read()
        if data:
            return data
    raise SystemExit("no secret: use --text, --secret-file, or pipe bytes on stdin")


def cmd_split(args) -> int:
    secret = _read_secret(args)
    result = service.split(secret, args.threshold, args.total)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        print(f"wrote {args.total} shares to {args.out}", file=sys.stderr)
    else:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    print(
        f"fingerprint: {result['secret_fingerprint']}  "
        f"(pass to recover with --expect-fingerprint to detect tampering)",
        file=sys.stderr,
    )
    return 0


def _load_shares(path) -> list:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, dict) and isinstance(data.get("shares"), list):
        return data["shares"]
    if isinstance(data, list):
        shares = []
        for item in data:
            if isinstance(item, str):
                shares.append(item)
            elif isinstance(item, dict) and isinstance(item.get("share"), str):
                shares.append(item["share"])
            elif isinstance(item, dict):
                shares.append(item)
            else:
                raise SystemExit(f"unsupported share entry in {path}")
        return shares
    raise SystemExit(f"could not find a shares list in {path}")


def cmd_recover(args) -> int:
    shares = _load_shares(args.shares_file)
    if args.share:
        shares.extend(args.share)
    expected_fp = args.expect_fingerprint
    result = service.recover_from_encoded(shares, expected_fingerprint=expected_fp)
    if args.secret_out:
        with open(args.secret_out, "wb") as fh:
            fh.write(base64.b64decode(result["secret_b64"]))
        print(f"recovered {result['secret_length']} bytes -> {args.secret_out}", file=sys.stderr)
        print(json.dumps({k: v for k, v in result.items() if k != "secret_b64"}, indent=2), file=sys.stderr)
    else:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        print(
            f"recovered text: {base64.b64decode(result['secret_b64']).decode('utf-8', 'replace')}",
            file=sys.stderr,
        )
    return 0


def cmd_serve(args) -> int:
    from .server import create_server

    httpd = create_server(args.host, args.port)
    host, port = httpd.server_address[:2]
    print(f"threshold-shares {__version__} listening on http://{host}:{port}", file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down", file=sys.stderr)
    finally:
        httpd.server_close()
    return 0


def cmd_seal(args) -> int:
    passphrase = args.passphrase or _read_passphrase(args)
    print(seal_share(args.share, passphrase))
    return 0


def cmd_unseal(args) -> int:
    passphrase = args.passphrase or _read_passphrase(args)
    print(unseal_share(args.sealed, passphrase))
    return 0


def _read_passphrase(args) -> str:
    if args.passphrase_file:
        with open(args.passphrase_file, "r", encoding="utf-8") as fh:
            return fh.read().rstrip("\n")
    return input("passphrase: ")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="threshold_shares", description="Threshold share recovery service")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", help="run the local HTTP service")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8080)
    p_serve.set_defaults(func=cmd_serve)

    p_split = sub.add_parser("split", help="split a secret into shares")
    p_split.add_argument("-k", "--threshold", type=int, required=True, help="shares needed to recover (>=2)")
    p_split.add_argument("-n", "--total", type=int, required=True, help="total shares to create (<=255)")
    secret_group = p_split.add_mutually_exclusive_group()
    secret_group.add_argument("--text", help="secret as a UTF-8 string")
    secret_group.add_argument("--secret-file", help="read secret bytes from file")
    p_split.add_argument("--out", help="write JSON result to this file instead of stdout")
    p_split.set_defaults(func=cmd_split)

    p_rec = sub.add_parser("recover", help="recover a secret from a shares JSON file / --share tokens")
    p_rec.add_argument("shares_file", help="JSON file: split output, a list, or [{share: ...}]")
    p_rec.add_argument("--share", action="append", help="additional SSS1$ share token (repeatable)")
    p_rec.add_argument("--expect-fingerprint", help="sha256 fingerprint from split; enables tamper detection")
    p_rec.add_argument("--secret-out", help="write recovered bytes to this file instead of printing")
    p_rec.set_defaults(func=cmd_recover)

    p_seal = sub.add_parser("seal", help="wrap an encoded share with a passphrase (AES-GCM + scrypt)")
    p_seal.add_argument("share")
    p_seal.add_argument("--passphrase")
    p_seal.add_argument("--passphrase-file")
    p_seal.set_defaults(func=cmd_seal)

    p_unseal = sub.add_parser("unseal", help="unwrap a sealed share token")
    p_unseal.add_argument("sealed")
    p_unseal.add_argument("--passphrase")
    p_unseal.add_argument("--passphrase-file")
    p_unseal.set_defaults(func=cmd_unseal)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except service.ServiceError as exc:
        payload = {"error": {"code": exc.code, "message": str(exc), "details": exc.details}}
        print(json.dumps(payload, indent=2, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
