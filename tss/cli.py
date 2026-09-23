"""命令行入口：split / recover / validate / serve / gen-auth-key。

示例：
    python -m tss.cli split --text "hello" --threshold 3 --total 5
    python -m tss.cli recover --in shares.txt
    python -m tss.cli serve --host 127.0.0.1 --port 8080
"""

import argparse
import base64
import json
import sys

from . import core
from .envelope import generate_auth_key
from .errors import TSSError
from .service import run_server


def _load_auth_key(args):
    if args.auth_key_b64:
        return core.decode_auth_key(args.auth_key_b64)
    if args.auth_key_file:
        with open(args.auth_key_file, "r", encoding="ascii") as f:
            return core.decode_auth_key(f.read().strip())
    return None


def cmd_split(args) -> int:
    if args.secret_file:
        with open(args.secret_file, "rb") as f:
            secret = f.read()
    elif args.text is not None:
        secret = args.text.encode("utf-8")
    elif args.secret_b64 is not None:
        secret = base64.b64decode(args.secret_b64, validate=True)
    else:
        secret = sys.stdin.buffer.read()

    result = core.split(secret, args.threshold, args.total,
                        auth_key=_load_auth_key(args))
    if args.json:
        print(json.dumps({
            "split_id": result.split_id,
            "threshold": result.threshold,
            "total": result.total,
            "authenticated": result.authenticated,
            "shares": result.shares,
        }, ensure_ascii=False, indent=2))
    else:
        print(f"# split_id={result.split_id} t={result.threshold} "
              f"n={result.total} authenticated={result.authenticated}")
        for s in result.shares:
            print(s)
    return 0


def _read_share_lines(path) -> list:
    if path:
        with open(path, "r", encoding="ascii") as f:
            lines = f.readlines()
    else:
        lines = sys.stdin.readlines()
    shares = []
    for line in lines:
        line = line.strip()
        if line and not line.startswith("#"):
            shares.append(line)
    return shares


def cmd_recover(args) -> int:
    shares = _read_share_lines(args.infile)
    result = core.recover(shares, auth_key=_load_auth_key(args))
    if args.b64_out:
        sys.stdout.buffer.write(base64.standard_b64encode(result.secret))
        sys.stdout.buffer.write(b"\n")
    else:
        try:
            sys.stdout.buffer.write(result.secret)
            sys.stdout.buffer.write(b"\n")
        except BrokenPipeError:
            return 0
    if args.json:
        print(json.dumps({
            "threshold": result.threshold,
            "total": result.total,
            "split_id": result.split_id,
            "authenticated": result.authenticated,
            "used_share_count": result.used_share_count,
            "rejected": result.rejected,
        }, ensure_ascii=False, indent=2), file=sys.stderr)
    return 0


def cmd_validate(args) -> int:
    shares = _read_share_lines(args.infile)
    if len(shares) != 1:
        raise core.ParameterError(f"validate expects exactly 1 share, got {len(shares)}")
    info = core.validate(shares[0], auth_key=_load_auth_key(args))
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0


def cmd_gen_auth_key(_args) -> int:
    print(base64.standard_b64encode(generate_auth_key()).decode("ascii"))
    return 0


def cmd_serve(args) -> int:
    run_server(host=args.host, port=args.port)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tss",
        description="阈值份额恢复（Shamir over GF(2^8)，本地服务，无外部账号）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_split = sub.add_parser("split", help="拆分秘密")
    src = p_split.add_mutually_exclusive_group()
    src.add_argument("--text", help="UTF-8 文本秘密")
    src.add_argument("--secret-file", help="从文件读取秘密（二进制）")
    src.add_argument("--secret-b64", help="base64 编码的秘密")
    p_split.add_argument("--threshold", "-t", type=int, required=True)
    p_split.add_argument("--total", "-n", type=int, required=True)
    p_split.add_argument("--auth-key-b64", help="base64 HMAC 认证密钥（可选）")
    p_split.add_argument("--auth-key-file", help="从文件读取认证密钥")
    p_split.add_argument("--json", action="store_true", help="输出 JSON")
    p_split.set_defaults(func=cmd_split)

    p_rec = sub.add_parser("recover", help="从份额恢复秘密（每行一个份额）")
    p_rec.add_argument("--in", dest="infile", help="份额文件；缺省读 stdin")
    p_rec.add_argument("--auth-key-b64")
    p_rec.add_argument("--auth-key-file")
    p_rec.add_argument("--b64-out", action="store_true", help="以 base64 输出秘密")
    p_rec.add_argument("--json", action="store_true", help="把元数据打到 stderr")
    p_rec.set_defaults(func=cmd_recover)

    p_val = sub.add_parser("validate", help="校验单个份额信封")
    p_val.add_argument("--in", dest="infile")
    p_val.add_argument("--auth-key-b64")
    p_val.add_argument("--auth-key-file")
    p_val.set_defaults(func=cmd_validate)

    p_key = sub.add_parser("gen-auth-key", help="生成 32 字节 HMAC 认证密钥（base64）")
    p_key.set_defaults(func=cmd_gen_auth_key)

    p_serve = sub.add_parser("serve", help="启动本地 HTTP 服务")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8080)
    p_serve.set_defaults(func=cmd_serve)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except TSSError as exc:
        payload = {"error": exc.code, "detail": exc.message}
        for attr in ("suspect", "votes", "rejected"):
            val = getattr(exc, attr, None)
            if val:
                payload[attr] = val
        print(json.dumps(payload, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
