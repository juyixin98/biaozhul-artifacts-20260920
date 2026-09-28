"""Service-call example: hit a running FastAPI server with synthetic data.

Start the server first (separate terminal):
    uvicorn ipw_ate.api:app --port 8000
then:
    PYTHONPATH=src python examples/call_service.py
"""

from __future__ import annotations

import os
import sys

import httpx

from ipw_ate.synthetic import make_no_overlap_data, make_overlap_data

URL = os.environ.get("IPW_BASE_URL", "http://127.0.0.1:8000")


def _post(client: httpx.Client, name: str, data, n_splits: int = 5) -> None:
    payload = {
        "treatment": data.treatment.tolist(),
        "outcome": data.outcome.tolist(),
        "covariates": data.covariates.tolist(),
        "estimand": "ATE",
        "n_splits": n_splits,
        "request_id": f"example_{name}",
    }
    r = client.post(f"{URL}/analyze", json=payload, timeout=30)
    print(f"\n=== {name} (HTTP {r.status_code}) ===")
    body = r.json()
    if r.status_code == 200:
        print("decision:", body["diagnostic"]["decision"])
        print(f"estimate: {body['estimate']:+.4f}  "
              f"CI: [{body['ci']['lower']:+.3f}, {body['ci']['upper']:+.3f}]")
        print("caveat  :", body["caveat"][:90], "...")
    else:
        err = body["error"]
        print("rejected code:", err["code"])
        if err.get("diagnostic"):
            d = err["diagnostic"]
            print("decision:", d["decision"], "reasons:", d["reasons"])

    # Fetch the persisted aggregate record.
    rec = client.get(f"{URL}/runs/example_{name}")
    print("stored record:", rec.status_code, rec.json().get("decision"))


def main() -> int:
    try:
        with httpx.Client() as client:
            if client.get(f"{URL}/health").status_code != 200:
                raise ConnectionError
            _post(client, "good", make_overlap_data(1000, seed=101))
            _post(client, "no_overlap", make_no_overlap_data(800, seed=44),
                  n_splits=4)
    except Exception as exc:  # surface connection problems clearly
        print(f"could not reach server at {URL}: {exc}", file=sys.stderr)
        print("start it with: uvicorn ipw_ate.api:app --port 8000",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
