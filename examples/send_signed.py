"""带真实 HMAC-SHA256 签名的命令行客户端。

用法：
    # 发送单条
    python -m examples.send_signed fuse examples/data/ordered.jsonl \
        --base-url http://127.0.0.1:8000 --limit 1

    # 按文件中的出现顺序逐条发送（乱序文件即可演示“迟到重放”）
    python -m examples.send_signed fuse examples/data/shuffled.jsonl

    # 批量发送（整个数组一次 POST /fuse/batch）
    python -m examples.send_signed batch examples/data/shuffled.jsonl

    # 查询
    python -m examples.send_signed state
    python -m examples.send_signed trace
    python -m examples.send_signed rejections
    python -m examples.send_signed reset
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.crypto import sign_request  # noqa: E402
from app.config import settings  # noqa: E402


def _signed_headers(method: str, path: str, raw: bytes) -> dict[str, str]:
    headers, _ = sign_request(method, path, raw)
    headers["Content-Type"] = "application/json"
    return headers


def load_jsonl(path: str) -> list[dict]:
    with open(path, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["fuse", "batch", "state", "trace", "rejections", "reset"])
    ap.add_argument("file", nargs="?")
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--secret", default=None)
    args = ap.parse_args()

    secret = args.secret or os.environ.get("FUSER_HMAC_SECRET", settings.HMAC_SECRET)
    base = args.base_url.rstrip("/")
    # trust_env=False：忽略环境代理变量，对本地服务直连（避免缺少 socksio 时报错）
    client = httpx.Client(timeout=10.0, trust_env=False)

    def request(method: str, url_path: str, payload: dict | list | None = None) -> httpx.Response:
        raw = json.dumps(payload, separators=(",", ":")).encode() if payload is not None else b""
        headers, _ = sign_request(method, url_path, raw, secret=secret, timestamp=f"{time.time():.6f}")
        if method == "GET":
            return client.request(method, base + url_path)
        headers["Content-Type"] = "application/json"
        return client.request(method, base + url_path, content=raw, headers=headers)

    if args.command == "fuse":
        if not args.file:
            ap.error("fuse requires a JSONL file")
        msgs = load_jsonl(args.file)
        if args.limit:
            msgs = msgs[: args.limit]
        n_accept = 0
        for m in msgs:
            r = request("POST", "/fuse", m)
            body = r.json()
            ok = body.get("accepted", False)
            n_accept += bool(ok)
            tag = body.get("reason") or body.get("status")
            print(f"{r.status_code} {m['id']:>12} accepted={ok} {tag} replayed={body.get('replayed')}")
        print(f"accepted {n_accept}/{len(msgs)}")
        return

    if args.command == "batch":
        if not args.file:
            ap.error("batch requires a JSONL file")
        msgs = load_jsonl(args.file)
        r = request("POST", "/fuse/batch", {"measurements": msgs})
        body = r.json()
        print(f"{r.status_code} received={body.get('received')} accepted={body.get('accepted')} rejected={body.get('rejected')}")
        for item in body.get("results", []):
            print(f"  {item['id']:>12} accepted={item['accepted']} {item.get('reason') or item.get('status')} replayed={item.get('replayed')}")
        return

    if args.command == "state":
        print(json.dumps(request("GET", "/state").json(), indent=2, ensure_ascii=False))
        return
    if args.command == "trace":
        body = request("GET", "/trace").json()
        print(f"trace count={body['count']}")
        for s in body["steps"][-10:]:
            print(
                f"  {s['time']:7.3f} {s['id']:>12} nis={s['nis']:8.3f} "
                f"accepted={s['accepted']} innov={['%.4f' % v for v in s['innovation']]}"
            )
        return
    if args.command == "rejections":
        print(json.dumps(request("GET", "/rejections").json(), indent=2, ensure_ascii=False))
        return
    if args.command == "reset":
        print(json.dumps(request("POST", "/reset", {}).json(), indent=2, ensure_ascii=False))
        return


if __name__ == "__main__":
    main()
