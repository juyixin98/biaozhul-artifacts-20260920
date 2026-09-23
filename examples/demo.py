#!/usr/bin/env python3
"""端到端演示（真实 HTTP + 真实密码学验证），仅使用标准库。

用法：
    先启动服务： uvicorn ibc_teach.app:app --port 8000
    再执行：     python examples/demo.py [BASE_URL]

脚本依次演示：
  1. 无序通道正常生命周期（发送 -> 接收 -> 确认）；
  2. 高度超时退款（边界点 == timeout_height 即超时）；
  3. 顺序通道遇缺口等待，补齐后放行；
  4. 错误检查点（签名损坏）被拒绝。

每一步都打印请求体关键字段与响应。
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"


def call(method: str, path: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data,
        headers={"Content-Type": "application/json"}, method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def step(title):
    print("\n" + "=" * 72 + f"\n{title}\n" + "-" * 72)


def must(status_code, resp, *allowed):
    assert status_code in allowed, f"expected {allowed}, got {status_code}: {resp}"
    return resp


def main():
    must(*call("GET", "/health"), 200)
    print("service healthy:", BASE)

    # 为保证脚本可重复执行，通道名带时间戳。
    suffix = str(__import__("time").time_ns())[-8:]
    ca = f"demo-{suffix}"
    cb = f"demo-{suffix}"

    # ---------- 1. 无序通道正常流程 ----------
    step("1) unordered 通道：send -> finalize(a) -> recv -> finalize(b) -> ack")
    code, _ = call("POST", "/channels", {
        "ordering": "unordered", "version": "v1",
        "port_a": "port-a", "port_b": "port-b",
        "channel_a": ca, "channel_b": cb,
    })
    assert code in (201, 409), (code, _)

    code, packet = call("POST", "/packets/send", {
        "src_chain": "chain-a", "src_port": "port-a", "src_channel": ca,
        "timeout_height": 100, "timeout_time_ns": 0,
        "data_hex": "deadbeef", "amount": 50,
    })
    must(code, packet, 201)
    seq = packet["sequence"]
    print("sent packet:", seq, packet)

    code, blk = call("POST", "/chains/chain-a/blocks")
    ha = must(code, blk, 200)["height"]
    print("chain-a produced signed block height", ha)
    # 证明通过两个端点取：/keys 把语义键编码为 path，/proof 返回成员证明。
    # 真实部署中由链下 relayer 完成同样的读取与搬运。
    code, keyinfo = call(
        "GET", f"/keys?kind=commitment&port=port-a&channel={ca}&sequence={seq}"
    )
    path = must(code, keyinfo, 200)["path"]
    code, proof_doc = call(
        "GET", f"/chains/chain-a/proof?height={ha}&path={path}"
    )
    must(code, proof_doc, 200)
    recv_body = {
        "packet": {
            "src_chain": "chain-a", "src_port": "port-a", "src_channel": ca,
            "dst_chain": "chain-b", "dst_port": "port-b", "dst_channel": cb,
            "sequence": seq, "timeout_height": 100, "timeout_time_ns": 0,
            "data_hex": "deadbeef", "amount": 50,
        },
        "checkpoint": proof_doc["checkpoint"],
        "proof": proof_doc["proof"],
    }
    code, resp = call("POST", "/packets/recv", recv_body)
    must(code, resp, 200)
    print("recv result:", resp)

    code, blk = call("POST", "/chains/chain-b/blocks")
    hb = must(code, blk, 200)["height"]
    code, keyinfo = call(
        "GET", f"/keys?kind=receipt&port=port-b&channel={cb}&sequence={seq}"
    )
    path = must(code, keyinfo, 200)["path"]
    code, proof_doc = call("GET", f"/chains/chain-b/proof?height={hb}&path={path}")
    ack_body = {
        "packet": recv_body["packet"],
        "checkpoint": proof_doc["checkpoint"],
        "proof": proof_doc["proof"],
    }
    code, resp = call("POST", "/packets/ack", ack_body)
    must(code, resp, 200)
    print("ack result:", resp)

    # ---------- 2. 高度超时退款 ----------
    step("2) timeout height 边界：目的链高度 == timeout_height 时拒绝接收并退款")
    code, packet2 = call("POST", "/packets/send", {
        "src_chain": "chain-a", "src_port": "port-a", "src_channel": ca,
        "timeout_height": hb + 1, "timeout_time_ns": 0,
        "data_hex": "0102", "amount": 9,
    })
    seq2 = must(code, packet2, 201)["sequence"]
    code, blk = call("POST", "/chains/chain-a/blocks")
    ha2 = must(code, blk, 200)["height"]
    # 推进目的链到超时高度。
    code, blk = call("POST", "/chains/chain-b/blocks")
    tip_b = must(code, blk, 200)["height"]
    assert tip_b >= hb + 1
    code, keyinfo = call(
        "GET", f"/keys?kind=commitment&port=port-a&channel={ca}&sequence={seq2}"
    )
    path = keyinfo["path"]
    code, proof_doc = call("GET", f"/chains/chain-a/proof?height={ha2}&path={path}")
    code, resp = call("POST", "/packets/recv", {
        "packet": {**recv_body["packet"], "sequence": seq2,
                   "timeout_height": hb + 1, "data_hex": "0102", "amount": 9},
        "checkpoint": proof_doc["checkpoint"], "proof": proof_doc["proof"],
    })
    print("recv at/after timeout ->", code, resp)
    must(code, resp, 408)

    # 非成员证明：回执不存在。
    code, keyinfo = call(
        "GET", f"/keys?kind=receipt&port=port-b&channel={cb}&sequence={seq2}"
    )
    code, proof_doc = call(
        "GET", f"/chains/chain-b/proof?height={tip_b}&path={keyinfo['path']}"
    )
    assert proof_doc["exists"] is False
    code, resp = call("POST", "/packets/timeout", {
        "packet": {**recv_body["packet"], "sequence": seq2,
                   "timeout_height": hb + 1, "data_hex": "0102", "amount": 9},
        "checkpoint": proof_doc["checkpoint"], "proof": proof_doc["proof"],
    })
    must(code, resp, 200)
    print("timeout refund result:", resp)

    # ---------- 3. 顺序通道缺口 ----------
    step("3) ordered 通道：seq2 先到必须等待（409 sequence_gap），seq1 到达后放行")
    oa, ob = f"ord-{suffix}", f"ord-{suffix}"
    code, _ = call("POST", "/channels", {
        "ordering": "ordered", "version": "v2",
        "channel_a": oa, "channel_b": ob,
    })
    assert code in (201, 409), (code, _)
    ps = []
    for data in ("11", "22", "33"):
        code, p = call("POST", "/packets/send", {
            "src_chain": "chain-a", "src_port": "port-a", "src_channel": oa,
            "timeout_height": 0, "timeout_time_ns": 0,
            "data_hex": data, "amount": 1,
        })
        ps.append(must(code, p, 201))
    code, blk = call("POST", "/chains/chain-a/blocks")
    ha3 = must(code, blk, 200)["height"]

    def recv_ordered(p):
        code, ki = call(
            "GET", f"/keys?kind=commitment&port=port-a&channel={oa}&sequence={p['sequence']}"
        )
        code, pd = call("GET", f"/chains/chain-a/proof?height={ha3}&path={ki['path']}")
        body = {
            "packet": {
                "src_chain": "chain-a", "src_port": "port-a", "src_channel": oa,
                "dst_chain": "chain-b", "dst_port": "port-b", "dst_channel": ob,
                "sequence": p["sequence"], "timeout_height": 0, "timeout_time_ns": 0,
                "data_hex": p["data_hex"], "amount": 1,
            },
            "checkpoint": pd["checkpoint"], "proof": pd["proof"],
        }
        return call("POST", "/packets/recv", body)

    code, resp = recv_ordered(ps[1])  # seq2 先到
    print("deliver seq2 before seq1 ->", code, resp)
    must(code, resp, 409)
    for p in ps:
        code, resp = recv_ordered(p)
        print(f"deliver seq{p['sequence']} in order ->", code, resp)
        must(code, resp, 200)

    # ---------- 4. 损坏检查点被拒 ----------
    step("4) 损坏的检查点签名必须被拒绝")
    code, packet3 = call("POST", "/packets/send", {
        "src_chain": "chain-a", "src_port": "port-a", "src_channel": ca,
        "timeout_height": 0, "timeout_time_ns": 0, "data_hex": "ff", "amount": 1,
    })
    seq3 = must(code, packet3, 201)["sequence"]
    code, blk = call("POST", "/chains/chain-a/blocks")
    ha4 = must(code, blk, 200)["height"]
    code, ki = call(
        "GET", f"/keys?kind=commitment&port=port-a&channel={ca}&sequence={seq3}"
    )
    code, pd = call("GET", f"/chains/chain-a/proof?height={ha4}&path={ki['path']}")
    bad = dict(pd)
    bad["checkpoint"] = dict(pd["checkpoint"], signature="00" * 64)
    code, resp = call("POST", "/packets/recv", {
        "packet": {**recv_body["packet"], "sequence": seq3,
                   "timeout_height": 0, "data_hex": "ff", "amount": 1},
        "checkpoint": bad["checkpoint"], "proof": bad["proof"],
    })
    print("bad checkpoint ->", code, resp)
    must(code, resp, 400)

    print("\nDEMO OK: 所有场景表现符合预期。")


if __name__ == "__main__":
    main()
