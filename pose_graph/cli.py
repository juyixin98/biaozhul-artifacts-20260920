"""Command-line entry point: ``python -m pose_graph.cli request.json``.

Reads a JSON optimization request, runs the solver, and prints the JSON
response to stdout (or ``-o/--output`` file).  Exit code is 0 on success,
2 on a malformed request / structure error.
"""

import argparse
import json
import sys

from .json_io import dump_response, load_request, run_from_dict


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="2D (SE2) pose graph optimization from a JSON request."
    )
    parser.add_argument("request", help="path to the JSON request file")
    parser.add_argument(
        "-o", "--output", help="write response JSON here instead of stdout"
    )
    args = parser.parse_args(argv)

    try:
        data = load_request(args.request)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"failed to read request: {exc}", file=sys.stderr)
        return 2

    response = run_from_dict(data)
    if args.output:
        dump_response(response, args.output)
        print(
            f"status={response['status']} "
            f"iterations={response.get('summary', {}).get('iterations')} "
            f"-> {args.output}",
            file=sys.stderr,
        )
    else:
        json.dump(response, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
    return 0 if response["status"] == "ok" else 2


if __name__ == "__main__":
    sys.exit(main())
