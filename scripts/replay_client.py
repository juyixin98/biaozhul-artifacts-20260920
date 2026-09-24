"""零第三方依赖的真实 HTTP 回放客户端（标准库 urllib）。

功能：
1. 创建会话（拿到 HMAC 密钥）；
2. 逐帧 POST 示例 JSON；
3. 用密钥真实校验每个响应的 HMAC-SHA256 签名；
4. 把第一帧再 POST 一次，证明重复投递幂等、next_track_id 不增长。

用法：
    uvicorn app.main:app --port 8000 &
    python scripts/replay_client.py examples/frames_duplicates.json \\
        --base-url http://127.0.0.1:8000
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.crypto import canonical_json  # noqa: E402


def request(method: str, url: str, body: dict | None = None) -> tuple[int, dict]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def verify(body: dict, key_hex: str) -> bool:
    sig = body.get("signature", "")
    unsigned = {k: v for k, v in body.items() if k != "signature"}
    expected = hmac.new(
        bytes.fromhex(key_hex),
        canonical_json(unsigned).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(sig, expected)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("frames_json", type=Path)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--session-id", default="replay-demo")
    args = parser.parse_args()

    frames = json.loads(args.frames_json.read_text(encoding="utf-8"))

    code, session = request(
        "POST",
        f"{args.base_url}/sessions",
        {"session_id": args.session_id},
    )
    if code not in (201, 409):
        print(f"创建会话失败: {code} {session}", file=sys.stderr)
        return 1
    if code == 409:
        # 会话已存在：换一个随机后缀重新创建以便拿到密钥
        import secrets

        args.session_id = f"replay-{secrets.token_hex(4)}"
        code, session = request(
            "POST", f"{args.base_url}/sessions", {"session_id": args.session_id}
        )
    key = session["signing_key_hex"]
    print(f"会话 {args.session_id} 已创建，HMAC 密钥已获取")

    last_next_id = None
    for frame in frames:
        code, body = request(
            "POST",
            f"{args.base_url}/sessions/{args.session_id}/frames",
            frame,
        )
        assert code == 200, (code, body)
        assert verify(body, key), "HMAC 签名校验失败"
        last_next_id = body["next_track_id"]
        print(
            f"frame={body['frame_id']:>3} dt={body['dt']} "
            f"tracks={len(body['tracks'])} births={body['births']} "
            f"matched={body['matched']} next_id={last_next_id} "
            f"hmac=ok"
        )

    # 重复投递第一帧：必须 replay=True 且 next_track_id 不变
    code, body = request(
        "POST",
        f"{args.base_url}/sessions/{args.session_id}/frames",
        frames[0],
    )
    assert code == 200, (code, body)
    assert body["replay"] is True, "重复帧必须返回 replay=true"
    assert body["next_track_id"] == last_next_id, "重复帧导致 ID 增长！"
    assert verify(body, key)
    print(
        f"重复投递 frame_id=0 -> replay=True, next_track_id 保持 "
        f"{last_next_id}，HMAC 校验通过"
    )

    # 乱序帧：frame_id 前进但 timestamp 回退，必须被拒绝（422）
    last_frame = frames[-1]
    stale = {
        "frame_id": last_frame["frame_id"] + 100,
        "timestamp": 0.0,
        "detections": [],
    }
    code, body = request(
        "POST",
        f"{args.base_url}/sessions/{args.session_id}/frames",
        stale,
    )
    assert code == 422, (code, body)
    print(f"乱序时间戳探测 -> 422 {body['detail']['error']}")

    # 已处理 frame_id 但负载不同 -> 幂等冲突 409
    code, body = request(
        "POST",
        f"{args.base_url}/sessions/{args.session_id}/frames",
        {"frame_id": 0, "timestamp": 0.0, "detections": []},
    )
    assert code == 409, (code, body)
    print(f"同 frame_id 不同负载 -> 409 {body['detail']['error']}")
    print("回放验收通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
