"""Send the sample request to a locally running server and print the result.

Usage:
    uvicorn app.main:app --port 8000          # in one terminal
    python examples/run_example.py            # in another
"""

import json
import os
import sys
from pathlib import Path

import httpx

URL = os.environ.get("SMOOTHER_URL", "http://127.0.0.1:8000") + "/smooth"


def main() -> int:
    payload = json.loads((Path(__file__).parent / "example_request.json").read_text())
    try:
        resp = httpx.post(URL, json=payload, timeout=10.0)
    except httpx.ConnectError:
        print("Cannot connect to %s — is the server running? (uvicorn app.main:app)" % URL)
        return 1
    print(f"HTTP {resp.status_code}")
    print(json.dumps(resp.json(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
