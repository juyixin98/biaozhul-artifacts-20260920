#!/usr/bin/env python3
"""Acceptance helper: verify a server response's integrity hashes locally.

Usage:
    curl -s http://127.0.0.1:8000/api/v1/evaluate \
        -H 'content-type: application/json' \
        --data @examples/pure_translation.json | python scripts/verify_response.py

Checks that:
  * integrity.response_sha256 == SHA-256(canonical JSON of the result block)
  * (optional) integrity.hmac_sha256 validates with TRAJECTORY_EVAL_HMAC_KEY
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.integrity import canonical_json  # noqa: E402


def main() -> int:
    body = json.load(sys.stdin)
    integrity = body.pop("integrity", None)
    if integrity is None:
        print("FAIL: response has no integrity block", file=sys.stderr)
        return 1

    payload = body
    digest = hashlib.sha256(canonical_json(payload)).hexdigest()
    ok = True

    if hmac.compare_digest(digest, integrity.get("response_sha256", "")):
        print(f"OK   response_sha256 ({digest[:16]}...)")
    else:
        print(f"FAIL response_sha256: computed {digest}", file=sys.stderr)
        ok = False

    if "request_sha256" in integrity:
        print(f"INFO request_sha256  {integrity['request_sha256'][:16]}...")

    key = os.environ.get("TRAJECTORY_EVAL_HMAC_KEY")
    if "hmac_sha256" in integrity:
        if not key:
            print("SKIP hmac: set TRAJECTORY_EVAL_HMAC_KEY to verify", file=sys.stderr)
        else:
            want = hmac.new(key.encode(), canonical_json(payload), hashlib.sha256).hexdigest()
            if hmac.compare_digest(want, integrity["hmac_sha256"]):
                print("OK   hmac_sha256")
            else:
                print(f"FAIL hmac_sha256: computed {want}", file=sys.stderr)
                ok = False

    if "error" in payload:
        print(f"NOTE server reported error: {payload['error']['code']}")
    else:
        m = payload["match"]
        print(
            f"INFO matched {m['n_matched']}/{m['n_estimated']} est, "
            f"{m['n_matched']}/{m['n_ground_truth']} gt; "
            f"coverage={m['coverage']:.3f}; "
            f"ATE_t_rmse={payload['ate']['translation_rmse']:.3e}; "
            f"ATE_R_rmse={payload['ate']['rotation_rmse_deg']:.3e} deg; "
            f"RPE pairs={payload['rpe']['n_pairs']}"
        )

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
