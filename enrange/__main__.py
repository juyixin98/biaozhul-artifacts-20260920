"""Command line entry point: ``python -m enrange ...``.

Subcommands
-----------
keygen --data-dir DIR                  create a local random 0600 master key
serve  --data-dir DIR [--host --port]  run the local HTTP service
put    --data-dir DIR [--id ID] --in FILE [--block-size N]
                                       encrypt a file into the store
get    --data-dir DIR --id ID [--start S --end E] [--out FILE]
                                       authenticated range read (half-open)
stat   --data-dir DIR --id ID          show authenticated metadata
list   --data-dir DIR                  list object ids
"""

from __future__ import annotations

import argparse
import sys

from .errors import AuthenticationError, InvalidRangeError, NotFoundError
from .store import DEFAULT_BLOCK_SIZE, MAX_BLOCK_SIZE, ObjectStore, new_object_id


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="enrange", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    keygen = sub.add_parser("keygen", help="create a local random master key")
    keygen.add_argument("--data-dir", required=True)

    serve = sub.add_parser("serve", help="run the local HTTP service")
    serve.add_argument("--data-dir", required=True)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)

    put = sub.add_parser("put", help="encrypt and store a file")
    put.add_argument("--data-dir", required=True)
    put.add_argument("--id", default=None, help="32 hex chars; random if omitted")
    put.add_argument("--in", dest="infile", required=True)
    put.add_argument("--block-size", type=int, default=DEFAULT_BLOCK_SIZE)

    get = sub.add_parser("get", help="authenticated (range) read")
    get.add_argument("--data-dir", required=True)
    get.add_argument("--id", required=True)
    get.add_argument("--start", type=int, default=0)
    get.add_argument(
        "--end",
        type=int,
        default=None,
        help="exclusive; omit for end of object",
    )
    get.add_argument("--out", default=None, help="output file; stdout if omitted")

    stat = sub.add_parser("stat", help="authenticated metadata")
    stat.add_argument("--data-dir", required=True)
    stat.add_argument("--id", required=True)

    ls = sub.add_parser("list", help="list object ids")
    ls.add_argument("--data-dir", required=True)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    try:
        if args.command == "keygen":
            store = ObjectStore.open_or_create(args.data_dir)
            print(f"master key ready at {args.data_dir.rstrip('/')}/master.key")
            print(f"key fingerprint (sha256, first 16 bytes): "
                  f"{__import__('hashlib').sha256(store.master_key).hexdigest()[:32]}")
            return 0

        store = ObjectStore.open_or_create(args.data_dir)

        if args.command == "serve":
            from .server import build_server

            httpd = build_server(store, host=args.host, port=args.port)
            print(f"enrange listening on http://{args.host}:{args.port}", flush=True)
            try:
                httpd.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                httpd.server_close()
            return 0

        if args.command == "put":
            if not 1 <= args.block_size <= MAX_BLOCK_SIZE:
                raise ValueError(f"block_size must be in [1, {MAX_BLOCK_SIZE}]")
            object_id = args.id or new_object_id()
            with open(args.infile, "rb") as fh:
                data = fh.read()
            meta = store.put(object_id, data, block_size=args.block_size)
            print(meta)
            return 0

        if args.command == "stat":
            print(store.stat(args.id))
            return 0

        if args.command == "list":
            for name in sorted(store.objects_dir.glob("*.bin")):
                print(name.name[:-4])
            return 0

        if args.command == "get":
            data = store.read_range(args.id, args.start, args.end)
            if args.out:
                with open(args.out, "wb") as fh:
                    fh.write(data)
                print(f"wrote {len(data)} bytes to {args.out}")
            else:
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()
            return 0

    except NotFoundError as exc:
        print(f"not found: {exc}", file=sys.stderr)
        return 3
    except AuthenticationError as exc:
        print(f"AUTHENTICATION FAILED: {exc}", file=sys.stderr)
        return 4
    except InvalidRangeError as exc:
        print(f"invalid range: {exc}", file=sys.stderr)
        return 5
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
