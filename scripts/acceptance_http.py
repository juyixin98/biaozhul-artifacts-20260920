"""端到端 HTTP 验收：启动真实 uvicorn 服务后运行（不是 mock、不是 ASGI 直调）。

检查内容：
  1. /healthz 与 /api/v1/info 可用；
  2. 对 examples/*.request.json 逐一发 POST，响应里的 request_sha256
     必须与本脚本用 hashlib 独立复算的 SHA-256 完全一致；
  3. 加载对应人工真值，调用 /api/v1/evaluate，打印并断言精确率/召回率；
  4. 陡坡必须全部 undecidable（不把最大平面当地面）；
  5. 非法请求返回 422；
  6. 服务端配置了 GROUNDSEG_HMAC_KEY 时，独立复算 HMAC-SHA256 验签通过，
     篡改后验签失败。

用法：
    python3 scripts/acceptance_http.py [BASE_URL]
默认 http://127.0.0.1:8000
"""

from __future__ import annotations

import glob
import hashlib
import hmac
import json
import os
import sys
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
EX_DIR = os.path.join(ROOT, "examples")

# 每个场景的验收门槛（与 tests/test_scene_contracts.py 保持一致）
THRESHOLDS = {
    "flat_ground":       {"precision": 0.99, "recall": 0.99, "must_abstain": False},
    "sloped_ground":     {"precision": 0.99, "recall": 0.98, "must_abstain": False},
    "ground_with_wall":  {"precision": 0.99, "recall": 0.99, "must_abstain": False},
    "ground_with_noise": {"precision": 0.99, "recall": 0.99, "must_abstain": False},
    "duplicate_points":  {"precision": 1.00, "recall": 1.00, "must_abstain": False},
    "steep_slope":       {"precision": None, "recall": None, "must_abstain": True},
    "sparse_ground":     {"precision": None, "recall": None, "must_abstain": True},
    "diffuse_noise":     {"precision": None, "recall": None, "must_abstain": True},
}

failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  ✓ " if cond else "  ✗ ") + msg)
    if not cond:
        failures.append(msg)


def post_json(url: str, raw: bytes, expect_status: int = 200):
    req = urllib.request.Request(
        url, data=raw, headers={"content-type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read()
            status = resp.status
    except urllib.error.HTTPError as e:
        body = e.read()
        status = e.code
    if status != expect_status:
        raise AssertionError(f"{url} 期望 {expect_status}，实际 {status}: {body[:200]!r}")
    return json.loads(body)


def get_json(url: str):
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read())


def main() -> int:
    base = sys.argv[1] if len(sys.argv) > 1 else os.environ.get(
        "GROUNDSEG_BASE_URL", "http://127.0.0.1:8000")

    print(f"== 对 {base} 执行 HTTP 验收 ==")
    h = get_json(base + "/healthz")
    check(h.get("status") == "ok", "/healthz 返回 ok")
    info = get_json(base + "/api/v1/info")
    check(info.get("defaults", {}).get("max_tilt_deg") == 20.0,
          "/api/v1/info 返回默认参数")

    request_files = sorted(glob.glob(os.path.join(EX_DIR, "*.request.json")))
    check(len(request_files) >= 8, f"发现 {len(request_files)} 个示例请求文件")

    hmac_key = os.environ.get("GROUNDSEG_HMAC_KEY")

    for rf in request_files:
        stem = os.path.basename(rf).replace(".request.json", "")
        scene = stem.replace(".loose", "")
        raw = open(rf, "rb").read()

        seg = post_json(base + "/api/v1/segment", raw)
        local_sha = hashlib.sha256(raw).hexdigest()
        check(seg["request_sha256"] == local_sha,
              f"[{stem}] request_sha256 与独立复算一致")

        if stem.endswith(".loose"):
            check(seg["stats"]["ground"] > 0,
                  f"[{stem}] 放宽倾角后分出地面 {seg['stats']['ground']} 点")
            continue

        truth_file = os.path.join(EX_DIR, f"{scene}.truth.json")
        if not os.path.exists(truth_file):
            continue
        truth = json.load(open(truth_file, encoding="utf-8"))["truth"]
        predicted = [p["label"] for p in seg["points"]]
        check(len(predicted) == len(truth),
              f"[{scene}] 返回点数 {len(predicted)} == 真值点数 {len(truth)}")

        ev_body = {"predicted": predicted, "truth": truth}
        ev = post_json(base + "/api/v1/evaluate",
                       json.dumps(ev_body).encode("utf-8"))
        check(ev.get("request_sha256") == hashlib.sha256(
            json.dumps(ev_body).encode()).hexdigest(),
            f"[{scene}] /evaluate 返回的请求指纹与独立复算一致")

        g = ev["metrics"]["ground"]
        thr = THRESHOLDS.get(scene)
        print(f"    [{scene:18s}] P={g['precision']:.3f} R={g['recall']:.3f} "
              f"undecided={g['undecided_rate']*100:.1f}% "
              f"(tp={g['tp']} fp={g['fp']} fn={g['fn']})")
        if thr is not None:
            if thr["must_abstain"]:
                check(g["undecided_rate"] == 1.0 and seg["stats"]["ground"] == 0,
                      f"[{scene}] 场景无可靠地面，100% 拒判为 undecidable")
            else:
                check(g["precision"] >= thr["precision"],
                      f"[{scene}] 精确率 {g['precision']:.3f} >= {thr['precision']}")
                check(g["recall"] >= thr["recall"],
                      f"[{scene}] 召回率 {g['recall']:.3f} >= {thr['recall']}")

        if hmac_key:
            sig = seg.get("signature")
            check(sig is not None and sig["algorithm"] == "HMAC-SHA256",
                  f"[{scene}] 响应带 HMAC-SHA256 签名")
            if sig:
                payload = {k: v for k, v in seg.items() if k != "signature"}
                canon = json.dumps(payload, sort_keys=True,
                                   separators=(",", ":"), ensure_ascii=False).encode()
                expected = hmac.new(hmac_key.encode(), canon,
                                    hashlib.sha256).hexdigest()
                check(hmac.compare_digest(sig["mac"], expected),
                      f"[{scene}] HMAC 独立复算验签通过")
                payload["n_points"] += 1
                tampered = hmac.new(hmac_key.encode(),
                                    json.dumps(payload, sort_keys=True,
                                               separators=(",", ":"),
                                               ensure_ascii=False).encode(),
                                    hashlib.sha256).hexdigest()
                check(not hmac.compare_digest(sig["mac"], tampered),
                      f"[{scene}] 篡改响应后验签失败（符合预期）")

    # 非法请求
    print("-- 非法请求 --")
    bad = [
        (b'{"points": [[1, 2]]}', "点维数错误"),
        (b'{"points": [[1, 2, NaN]]}', "NaN 坐标"),
        (b'{"points": [], "point_ids": ["x"]}', "ID 长度不匹配"),
        (b'{"predicted": ["sky"], "truth": [1]}', "非法标签"),
    ]
    for body, desc in bad:
        url = (base + "/api/v1/evaluate" if b"predicted" in body
               else base + "/api/v1/segment")
        try:
            post_json(url, body, expect_status=422)
            check(True, f"{desc} -> 422")
        except AssertionError:
            check(False, f"{desc} -> 422")

    print()
    if failures:
        print(f"验收失败：{len(failures)} 项")
        for f in failures:
            print("  FAIL:", f)
        return 1
    print("全部 HTTP 验收项通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
