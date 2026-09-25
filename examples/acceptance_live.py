"""Manual acceptance run against a live server: valid, replay, tamper, bursts.

Usage: python3 examples/acceptance_live.py [base_url]
"""

from __future__ import annotations

import http.client
import json
import sys
import threading
import time
from urllib.parse import urlsplit

from replay_protection.keys import KeyRegistry
from replay_protection.signing import SIGNING_ALGORITHM, canonical_request, sign_request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080"
KID = "test-key-1"


def call(method: str, target: str, body: bytes, headers: dict[str, str]) -> tuple[int, dict]:
    u = urlsplit(BASE)
    conn = http.client.HTTPConnection(u.hostname, u.port, timeout=5)
    conn.request(method, target, body=body, headers=headers)
    r = conn.getresponse()
    data = r.read()
    conn.close()
    try:
        return r.status, json.loads(data)
    except json.JSONDecodeError:
        return r.status, {"raw": data.decode("utf-8", "replace")}


def main() -> int:
    reg = KeyRegistry.load("data/keys.json")
    secret = reg.get(KID)
    ts = int(time.time())
    nonce_seq = 0

    def make(target="/v1/verify", body=b"{}", *, nonce=None, ts_override=None):
        nonlocal nonce_seq
        nonce_seq += 1
        n = nonce or f"accept-{nonce_seq:06d}".ljust(24, "x")
        t = ts if ts_override is None else ts_override
        canon = canonical_request("POST", target, body, key_id=KID, timestamp=t, nonce=n)
        sig = sign_request(secret, canon)
        h = {
            "X-Key-Id": KID,
            "X-Timestamp": str(t),
            "X-Nonce": n,
            "Authorization": f"{SIGNING_ALGORITHM} Credential={KID}, Signature={sig}",
            "Content-Type": "application/json",
        }
        return h, n, t

    results = []

    def record(name, status, expect):
        ok = status == expect
        results.append(ok)
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: HTTP {status} (expected {expect})")

    # 1. health
    record("GET /healthz open", call("GET", "/healthz", b"", {})[0], 200)

    # 2. valid signed request
    body = b'{"op":"transfer","amount":42}'
    h, n, t = make(body=body)
    record("valid signed POST", call("POST", "/v1/verify", body, h)[0], 200)

    # 3. exact replay -> 403
    record("identical replay", call("POST", "/v1/verify", body, h)[0], 403)

    # 4. tampered body, same signature -> 401
    record("tampered body", call("POST", "/v1/verify", b'{"op":"transfer","amount":1}', h)[0], 401)

    # 5. tampered method -> signature mismatch (GET not routed; use signed POST replay-free)
    canon_get = canonical_request("GET", "/v1/verify", b"", key_id=KID, timestamp=t, nonce=n)
    # not routed anyway; instead verify path trick: encoded slash must not equal real slash
    h2, n2, _ = make(target="/v1/%63heck-other")
    # %63 = c -> canonicalizes to /v1/check-other which does not exist
    st, payload = call("POST", "/v1/%63heck-other", b"", h2)
    record("canonicalized unknown route is 404 not bypass", st, 404)

    # 6. time boundaries. Exact +/-300 endpoints are verified deterministically
    # in the unit tests (injected clock); over real HTTP a second can tick, so
    # the live run checks one second inside plus the hard out-of-window cases.
    for delta, expect in [(-299, 200), (299, 200), (-301, 401), (301, 401)]:
        hb, _, _ = make(body=b"{}", ts_override=t + delta)
        record(f"timestamp delta {delta:+d}", call("POST", "/v1/verify", b"{}", hb)[0], expect)

    # 7. missing headers
    record("unsigned POST", call("POST", "/v1/verify", b"x", {"Content-Length": "1"})[0], 401)

    # 8. 40 concurrent identical signed requests: exactly one 200
    burst_body = b'{"burst":true}'
    hb, nb, tb = make(body=burst_body)
    statuses: list[int] = []
    barrier = threading.Barrier(40)

    def fire():
        barrier.wait()
        statuses.append(call("POST", "/v1/verify", burst_body, hb)[0])

    threads = [threading.Thread(target=fire) for _ in range(40)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    wins = statuses.count(200)
    replays = statuses.count(403)
    ok = wins == 1 and replays == 39
    results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] 40 concurrent duplicates: {wins}x200 {replays}x403 "
          f"(expected 1x200 39x403) other={[s for s in statuses if s not in (200,403)]}")

    print()
    print(f"TOTAL: {sum(results)}/{len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
