"""Command-line interface for the feature pipeline (JSON in, JSON out).

Commands:
    fit        Fit a pipeline declared by a spec file on training data
    transform  Apply a saved fitted pipeline to new data
    demo       Run a reproducible synthetic-data end-to-end demonstration

Data files are JSON objects mapping column name -> list of values, using
JSON null for missing values. Spec files are JSON lists of ColumnSpec dicts.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np

from .exceptions import FeaturePipelineError
from .pipeline import ColumnSpec, FeaturePipeline
from .serialization import load_pipeline, save_pipeline


def _read_json(path: str | Path) -> Any:
    source = Path(path)
    if not source.exists():
        raise FeaturePipelineError(f"File not found: {source}")
    return json.loads(source.read_text(encoding="utf-8"))


def _column_from_values(values: list[Any], categorical: bool) -> np.ndarray:
    if categorical:
        return np.array([None if v is None else str(v) for v in values], dtype=object)
    return np.array(
        [np.nan if v is None else float(v) for v in values], dtype=float
    )


def _build_columns(raw_spec: list[dict]) -> list[ColumnSpec]:
    return [ColumnSpec(**item) for item in raw_spec]


def _materialize(
    payload: dict[str, list[Any]], specs: list[ColumnSpec]
) -> dict[str, np.ndarray]:
    kinds = {s.name: s.dtype == "categorical" for s in specs}
    return {
        name: _column_from_values(values, kinds.get(name, False))
        for name, values in payload.items()
    }


def _result_to_json(result: Any) -> dict[str, Any]:
    return {
        "feature_names_out": result.feature_names_out,
        "rows": result.X.tolist(),
        "unknown_categories": {
            name: mask.tolist() for name, mask in result.unknown_categories.items()
        },
    }


def _cmd_fit(args: argparse.Namespace) -> int:
    specs = _build_columns(_read_json(args.spec))
    data = _materialize(_read_json(args.data), specs)
    pipeline = FeaturePipeline(specs).fit(data)
    save_pipeline(pipeline, args.out)
    print(f"Fitted pipeline saved to {args.out}")
    print(f"Output features ({len(pipeline.feature_names_out)}): "
          f"{pipeline.feature_names_out}")
    return 0


def _cmd_transform(args: argparse.Namespace) -> int:
    pipeline = load_pipeline(args.model)
    specs = pipeline.columns
    data = _materialize(_read_json(args.data), specs)
    payload = _result_to_json(pipeline.transform(data))
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"Transform result saved to {args.out}")
    else:
        print(text)
    return 0


def _cmd_demo(args: argparse.Namespace) -> int:
    # Imported lazily so `fit`/`transform` do not pay the demo import cost.
    from .demo import run_demo

    run_demo(Path(args.outdir))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="feature-pipeline",
        description="Fit/apply a local NumPy-only feature transformation pipeline.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    fit = sub.add_parser("fit", help="Fit a pipeline on training data.")
    fit.add_argument("--spec", required=True, help="JSON list of ColumnSpec dicts.")
    fit.add_argument("--data", required=True, help="Training data JSON.")
    fit.add_argument("--out", required=True, help="Destination model JSON path.")
    fit.set_defaults(func=_cmd_fit)

    apply = sub.add_parser("transform", help="Transform data with a saved model.")
    apply.add_argument("--model", required=True, help="Saved pipeline JSON.")
    apply.add_argument("--data", required=True, help="Data to transform JSON.")
    apply.add_argument("--out", help="Optional result JSON path (default: stdout).")
    apply.set_defaults(func=_cmd_transform)

    demo = sub.add_parser("demo", help="Run the synthetic-data demonstration.")
    demo.add_argument("--outdir", default="demo_output", help="Output directory.")
    demo.set_defaults(func=_cmd_demo)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except FeaturePipelineError as exc:
        parser.exit(status=2, message=f"error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
