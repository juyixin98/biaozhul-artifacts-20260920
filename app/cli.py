"""Command-line one-shot analyzer.

Runs a batch of reachability queries against one snapshot without starting the
HTTP server. Input file format::

    {
      "snapshot": { "namespaces": [...], "pods": [...], "policies": [...] },
      "queries":  [ { "source": ..., "destination": ..., ... }, ... ]
    }

Usage::

    python -m app.cli examples/scenario.json
"""

from __future__ import annotations

import argparse
import json
import sys

from .analyzer import Analyzer, UnsupportedProtocolError, ValidationError
from .models import QueryRequest, Snapshot


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Offline NetworkPolicy reachability analysis (batch CLI)."
    )
    parser.add_argument("input", help="path to a JSON file with snapshot+queries")
    parser.add_argument(
        "--fail-on-deny",
        action="store_true",
        help="exit with code 2 when any query is not reachable",
    )
    args = parser.parse_args(argv)

    with open(args.input, "r", encoding="utf-8") as fh:
        doc = json.load(fh)

    if not isinstance(doc, dict) or "snapshot" not in doc or "queries" not in doc:
        print("input must contain 'snapshot' and 'queries' keys", file=sys.stderr)
        return 1

    analyzer = Analyzer(Snapshot.model_validate(doc["snapshot"]))

    results = []
    had_error = False
    for i, raw_query in enumerate(doc["queries"]):
        query = QueryRequest.model_validate(raw_query)
        try:
            result = analyzer.analyze(query)
        except UnsupportedProtocolError as exc:
            results.append({"index": i, "error": "unsupported_protocol",
                            "detail": str(exc)})
            had_error = True
            continue
        except ValidationError as exc:
            results.append({"index": i, "error": "validation_error",
                            "detail": str(exc)})
            had_error = True
            continue
        results.append({"index": i, **result})

    print(json.dumps(
        {"warnings": list(analyzer.warnings), "results": results},
        indent=2,
        ensure_ascii=False,
    ))

    if had_error:
        return 1
    if args.fail_on_deny and any(
        r.get("reachable") is False for r in results
    ):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
