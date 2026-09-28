#!/usr/bin/env python3
"""Local HTTP server entry point.

    python scripts/run_server.py            # 127.0.0.1:8000
    python scripts/run_server.py --port 9000

Then:
    curl -s localhost:8000/health
    curl -s -X POST localhost:8000/api/v1/eigendecomposition \
        -H 'content-type: application/json' \
        -d '{"matrix":[[4,1],[1,2]]}'
Interactive API docs are served at /docs.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import uvicorn  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    uvicorn.run(
        "sym_eig.api.app:app",
        host=args.host,
        port=args.port,
        reload=False,
    )


if __name__ == "__main__":
    main()
