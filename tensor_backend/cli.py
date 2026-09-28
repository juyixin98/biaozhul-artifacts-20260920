"""Command-line entry points.

Usage::

    python -m tensor_backend.cli validate     # run the NumPy-oracle suite
    python -m tensor_backend.cli demo         # build a small graph and print it
"""
from __future__ import annotations

import argparse
import json
import sys

import numpy as np

from .graph import ComputeGraph
from .tensor import Tensor
from .training import TrainingState
from .validation import run_validation_suite


def _cmd_validate(_: argparse.Namespace) -> int:
    report = run_validation_suite()
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 1 if (report["summary"]["failed"] or report["summary"]["errors"]) else 0


def _cmd_demo(_: argparse.Namespace) -> int:
    g = ComputeGraph(graph_id="demo")
    g.constant("x", np.arange(12).reshape(3, 4).tolist())
    g.transpose("xt", "x")
    g.reshape("xf", "xt", [12], allow_copy=True)
    print("tensors:", g.handles())
    print("trace:", json.dumps(g.trace(), indent=2, default=str))
    print("aliasing:", json.dumps(g.aliasing_report(), indent=2))

    state = TrainingState(learning_rate=0.1)
    state.init_linear(2, seed=1)
    x = Tensor.from_values([[1.0, 0.0], [0.0, 1.0]])
    y = Tensor.from_values([[2.0], [3.0]])
    losses = state.train(x, y, 50)
    print("training losses (first/last):", losses[0], losses[-1])
    print("final params:", state.snapshot()["parameters"])
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tensor_backend")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("validate").set_defaults(func=_cmd_validate)
    sub.add_parser("demo").set_defaults(func=_cmd_demo)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
