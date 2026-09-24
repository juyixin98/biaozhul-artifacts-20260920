#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
手工向运动命令仲裁服务发送一条真实 HMAC 签名请求。

自动读取服务器当前时钟（/v1/time）作为 issue_ms，因此无需手动对时。

示例：
  # 发送自主命令（vx=0.5，租约 2 秒）
  python3 scripts/send.py examples/config.example.json auto-1 command \\
      --vx 0.5 --lease-ms 2000

  # 发送遥控命令
  python3 scripts/send.py examples/config.example.json rc-1 command \\
      --vx 1.0 --omega 0.3 --lease-ms 1000

  # 按下 / 解除急停
  python3 scripts/send.py examples/config.example.json estop-1 estop --action trigger
  python3 scripts/send.py examples/config.example.json estop-1 estop --action clear

  # 从文件读取请求体（示例见 examples/payloads/）
  python3 scripts/send.py examples/config.example.json auto-1 command --file examples/payloads/command.json

  # 显式指定 seq / issue_ms（默认 seq 用本地递增计数，issue_ms 用服务器时钟）
  python3 scripts/send.py config.json auto-1 command --vx 0.2 --seq 42 --issue-ms 1700000000000
"""
import argparse
import hashlib
import hmac
import json
import os
import sys
import tempfile
import urllib.request
import urllib.error

DOMAINS = {
    "command": (b"motion-command-v1\n", "/v1/command"),
    "estop": (b"estop-command-v1\n", "/v1/estop"),
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("config", help="服务器使用的配置文件（从中取该来源 key_hex）")
    ap.add_argument("source", help="来源 ID，如 auto-1 / rc-1 / estop-1")
    ap.add_argument("kind", choices=["command", "estop"])
    ap.add_argument("--base", default=os.environ.get("ARBITER_BASE", "http://127.0.0.1:8080"))
    ap.add_argument("--seq", type=int, default=None)
    ap.add_argument("--issue-ms", type=int, default=None)
    ap.add_argument("--valid-ms", type=int, default=2000)
    # command only
    ap.add_argument("--vx", type=float, default=0.0)
    ap.add_argument("--vy", type=float, default=0.0)
    ap.add_argument("--omega", type=float, default=0.0)
    ap.add_argument("--lease-ms", type=int, default=2000)
    # estop only
    ap.add_argument("--action", choices=["trigger", "clear"], default="trigger")
    ap.add_argument("--file", help="从 JSON 文件读取完整请求体（忽略其他字段参数）")
    args = ap.parse_args()

    cfg = json.load(open(args.config, encoding="utf-8"))
    if args.source not in cfg["sources"]:
        sys.exit(f"来源 {args.source} 不在配置中，可用: {list(cfg['sources'])}")
    key = bytes.fromhex(cfg["sources"][args.source]["key_hex"])

    # seq 的本地持久计数，避免手工测试时 stale_sequence。
    state_path = os.path.join(tempfile.gettempdir(), f"motion-arbiter-seq-{args.source}")
    next_seq = args.seq
    if next_seq is None:
        try:
            next_seq = int(open(state_path).read()) + 1
        except OSError:
            next_seq = 1
    with open(state_path, "w") as f:
        f.write(str(next_seq))

    if args.file:
        body = json.load(open(args.file, encoding="utf-8"))
    else:
        issue_ms = args.issue_ms
        if issue_ms is None:
            with urllib.request.urlopen(args.base + "/v1/time", timeout=5) as r:
                issue_ms = json.loads(r.read())["now_ms"]
        if args.kind == "command":
            body = {
                "seq": next_seq,
                "nonce": f"{args.source}-{next_seq}-{os.getpid()}",
                "vx": args.vx, "vy": args.vy, "omega": args.omega,
                "issue_ms": issue_ms,
                "valid_for_ms": args.valid_ms,
                "lease_for_ms": args.lease_ms,
            }
        else:
            body = {
                "seq": next_seq,
                "nonce": f"{args.source}-{next_seq}-{args.action}-{os.getpid()}",
                "action": args.action,
                "issue_ms": issue_ms,
                "valid_for_ms": args.valid_ms,
            }

    domain, path = DOMAINS[args.kind]
    raw = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode()
    sig = hmac.new(key, domain + raw, hashlib.sha256).hexdigest()
    req = urllib.request.Request(
        args.base + path, data=raw, method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Source": args.source,
            "X-Signature": "sha256=" + sig,
        })
    print(f"POST {path}  source={args.source}  seq={body.get('seq')}")
    print("body:", raw.decode())
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            print(f"HTTP {r.status}")
            print(json.dumps(json.loads(r.read()), indent=2, ensure_ascii=False))
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}")
        print(e.read().decode())
        sys.exit(1)


if __name__ == "__main__":
    main()
