#!/usr/bin/env python3
"""Generate a SynapticGo linear-classifier weight blob.

Produces the exact binary layout the API expects in ``weights_base64``::

    uint32 input_dim, uint32 num_classes,
    float32 W[num_classes * input_dim]   (row-major, little-endian),
    float32 b[num_classes]               (little-endian)

Example
-------
A 2-class, 3-input model where class 0 scores x0-x1+0.5x2-0.25 and
class 1 scores x1-0.5x2+0.25::

    python3 examples/make_weights.py \\
        --input-dim 3 --classes cat dog \\
        --W 1 -1 0.5 0 1 -0.5 \\
        --b -0.25 0.25
"""
import argparse
import base64
import json
import struct
import urllib.request


def encode(input_dim: int, classes: list[str], w: list[float], b: list[float]) -> bytes:
    if len(w) != input_dim * len(classes):
        raise SystemExit(f"W must have {input_dim * len(classes)} values")
    if len(b) != len(classes):
        raise SystemExit(f"b must have {len(classes)} values")
    blob = struct.pack("<II", input_dim, len(classes))
    blob += b"".join(struct.pack("<f", v) for v in w)
    blob += b"".join(struct.pack("<f", v) for v in b)
    return blob


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input-dim", type=int, required=True)
    p.add_argument("--classes", nargs="+", required=True)
    p.add_argument("--W", dest="w", nargs="+", type=float, required=True)
    p.add_argument("--b", nargs="+", type=float, required=True)
    p.add_argument("--register-url", default="",
                   help="optional POST /models URL to register directly")
    p.add_argument("--api-key", default="")
    p.add_argument("--dataset-id", type=int, default=0)
    p.add_argument("--model-name", default="clf")
    args = p.parse_args()

    blob = encode(args.input_dim, args.classes, args.w, args.b)
    b64 = base64.b64encode(blob).decode()
    print(b64)

    if args.register_url:
        body = json.dumps({
            "model_name": args.model_name,
            "dataset_id": args.dataset_id,
            "input_dim": args.input_dim,
            "classes": args.classes,
            "weights_base64": b64,
        }).encode()
        req = urllib.request.Request(
            args.register_url, data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {args.api_key}"})
        with urllib.request.urlopen(req) as r:
            print(r.read().decode())


if __name__ == "__main__":
    main()
