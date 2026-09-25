"""Start the model-switch HTTP service.

Examples::

    # 1) create the synthetic registry (idempotent)
    python scripts/seed_artifacts.py --root ./artifacts

    # 2) serve, loading and warming up v1 before accepting traffic
    python scripts/serve.py --root ./artifacts --initial v1 --port 8000
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from model_switch.app import build_server  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Atomic model-switch service")
    parser.add_argument("--root", default="artifacts", help="artifact registry root")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--initial",
        default=None,
        help="version to load+warm up before serving (e.g. v1)",
    )
    args = parser.parse_args()

    server = build_server(args.host, args.port, args.root, initial=args.initial)
    active = server.manager.active_version  # type: ignore[attr-defined]
    print(
        f"model-switch service on http://{args.host}:{args.port} "
        f"(registry={args.root}, active={active})",
        flush=True,
    )
    print("endpoints: GET /health /status /versions | POST /switch /predict", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down ...", flush=True)
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
