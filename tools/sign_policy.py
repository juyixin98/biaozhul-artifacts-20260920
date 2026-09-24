"""Sign a policy document, producing a signed bundle JSON.

Usage::

    python -m tools.sign_policy \\
        --key examples/keys/demo.pem \\
        --document examples/policy.json \\
        --out examples/policy.signed.json

For a policy set, pass a document containing ``{"policies": [...]}``.
The output bundle can be used verbatim as the ``signed`` field of any API
request, e.g.::

    jq -n --slurpfile s examples/policy.signed.json \\
       '{signed: $s[0], subject: {...}, resource: {...}}'
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.signing import load_private_key_pem, sign_document


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key", required=True, help="Ed25519 private key PEM")
    parser.add_argument("--document", required=True,
                        help="policy/policy-set JSON to sign")
    parser.add_argument("--out", required=True, help="bundle output path")
    args = parser.parse_args()

    private_key = load_private_key_pem(Path(args.key).read_bytes())
    document = json.loads(Path(args.document).read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise SystemExit("document must be a JSON object")
    bundle = sign_document(document, private_key)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(bundle, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"signed bundle -> {out_path} (kid={bundle['kid'][:12]}...)")


if __name__ == "__main__":
    main()
