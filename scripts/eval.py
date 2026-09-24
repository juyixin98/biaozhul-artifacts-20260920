#!/usr/bin/env python3
"""组装并发送一次准入评估请求。

用法: eval.py <port> <场景名> <image.json> [--sbom FILE] [--verification FILE]
                                                  [--exemption FILE ...]
image/sbom/verification/exemptions 在线协议中均为 base64(JSON 原文)，
缺省的证据字段省略，由服务端按“缺证据不通过”处理。
"""
import argparse
import base64
import json
import sys
import urllib.request


def b64_file(path):
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("port")
    ap.add_argument("name")
    ap.add_argument("image")
    ap.add_argument("--sbom")
    ap.add_argument("--verification")
    ap.add_argument("--exemption", action="append", default=[])
    ap.add_argument("--expect", choices=["ALLOW", "DENY", "UNKNOWN"],
                    help="断言最终判定；不符则以退出码 2 失败")
    args = ap.parse_args()

    req = {"image": b64_file(args.image)}
    if args.sbom:
        req["sbom"] = b64_file(args.sbom)
    if args.verification:
        req["verification"] = b64_file(args.verification)
    if args.exemption:
        arr = b"[" + b",".join(open(p, "rb").read().strip() for p in args.exemption) + b"]"
        req["exemptions"] = base64.b64encode(arr).decode()

    http_req = urllib.request.Request(
        f"http://127.0.0.1:{args.port}/v1/admission/evaluate",
        data=json.dumps(req).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(http_req) as resp:
            report = json.load(resp)
    except urllib.error.HTTPError as e:
        print(f"=== {args.name} === HTTP {e.code}: {e.read().decode()}")
        return 1

    print(f"=== {args.name} ===")
    print(f"decision={report['decision']}  id={report['id']}  policy={report['policyVersion']}")
    for f in report["findings"]:
        ex = f"  [豁免 {f['exemptionId']}]" if f.get("exemptionId") else ""
        print(f"  [{f['status']:7}] {f['ruleId']:22} {f['reason']}{ex}")
    print()
    if args.expect and report["decision"] != args.expect:
        print(f"!! 断言失败: 期望 {args.expect}，实际 {report['decision']}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
