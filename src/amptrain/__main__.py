"""``python -m amptrain`` serves the FastAPI app with uvicorn."""

from __future__ import annotations

import argparse


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the amptrain mixed-precision API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    import uvicorn

    uvicorn.run("amptrain.api:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
