"""Neural network JSON definition: validation + torch model construction.

Supported layers (CPU only):
  - {"name": ..., "type": "dense",   "out_features": int, "input": <src>}
  - {"name": ..., "type": "relu",    "input": <src>}
  - {"name": ..., "type": "dropout", "p": float,         "input": <src>}

`input` names either another layer or the virtual source ``"input"``.
The graph must be a DAG with exactly one terminal (output) layer. Dense
layers accept a single input only — no multi-input merge layers.
"""
from __future__ import annotations

import hashlib
from collections import deque
from typing import Any

import torch
import torch.nn as nn

LAYER_TYPES = ("dense", "relu", "dropout")
INPUT_NODE = "input"

MAX_DENSE_FEATURES = 100_000
MAX_DROPOUT_P = 0.999  # p=1.0 zeros every activation and breaks training


class GraphValidationError(ValueError):
    pass


def normalize_spec(spec: Any) -> dict:
    """Validate a raw spec dict and return its canonical, normalized form."""
    if not isinstance(spec, dict):
        raise GraphValidationError("spec must be an object")

    layers = spec.get("layers")
    in_features = spec.get("input_features")
    if not isinstance(in_features, int) or isinstance(in_features, bool):
        raise GraphValidationError("input_features must be an integer")
    if not (1 <= in_features <= MAX_DENSE_FEATURES):
        raise GraphValidationError(
            f"input_features must be in [1, {MAX_DENSE_FEATURES}]"
        )
    if not isinstance(layers, list) or not layers:
        raise GraphValidationError("layers must be a non-empty list")

    norm_layers: list[dict] = []
    seen: set[str] = set()
    for i, layer in enumerate(layers):
        if not isinstance(layer, dict):
            raise GraphValidationError(f"layer #{i} must be an object")
        name = layer.get("name")
        ltype = layer.get("type")
        if not isinstance(name, str) or not name:
            raise GraphValidationError(f"layer #{i}: name must be a non-empty string")
        if name == INPUT_NODE:
            raise GraphValidationError(f"layer name {INPUT_NODE!r} is reserved")
        if name in seen:
            raise GraphValidationError(f"duplicate layer name: {name!r}")
        seen.add(name)
        if ltype not in LAYER_TYPES:
            raise GraphValidationError(
                f"layer {name!r}: type must be one of {LAYER_TYPES}, got {ltype!r}"
            )
        src = layer.get("input")
        if not isinstance(src, str) or not src:
            raise GraphValidationError(f"layer {name!r}: input must be a layer name")

        norm: dict[str, Any] = {"name": name, "type": ltype, "input": src}
        if ltype == "dense":
            out_features = layer.get("out_features")
            if (
                not isinstance(out_features, int)
                or isinstance(out_features, bool)
                or not (1 <= out_features <= MAX_DENSE_FEATURES)
            ):
                raise GraphValidationError(
                    f"layer {name!r}: out_features must be an integer in "
                    f"[1, {MAX_DENSE_FEATURES}]"
                )
            norm["out_features"] = out_features
        elif ltype == "dropout":
            p = layer.get("p")
            if not isinstance(p, (int, float)) or isinstance(p, bool):
                raise GraphValidationError(f"layer {name!r}: p must be a number")
            p = float(p)
            if not (0.0 <= p <= MAX_DROPOUT_P):
                raise GraphValidationError(
                    f"layer {name!r}: p must be in [0, {MAX_DROPOUT_P}]"
                )
            norm["p"] = p
        norm_layers.append(norm)

    # Input references must exist (layer or virtual input), no self loops.
    for layer in norm_layers:
        src = layer["input"]
        if src == layer["name"]:
            raise GraphValidationError(f"layer {layer['name']!r} cannot input itself")
        if src != INPUT_NODE and src not in seen:
            raise GraphValidationError(
                f"layer {layer['name']!r}: unknown input layer {src!r}"
            )

    order = _topological_order(norm_layers)
    _check_single_output(norm_layers)
    _propagate_shapes(norm_layers, in_features, order)

    return {"input_features": in_features, "layers": norm_layers}


def _topological_order(layers: list[dict]) -> list[str]:
    by_name = {l["name"]: l for l in layers}
    indegree = {name: 0 for name in by_name}
    children: dict[str, list[str]] = {name: [] for name in by_name}
    for l in layers:
        src = l["input"]
        if src in by_name:
            indegree[l["name"]] += 1
            children[src].append(l["name"])

    # Deterministic tie-breaking: order of declaration.
    queue = deque(l["name"] for l in layers if indegree[l["name"]] == 0)
    order: list[str] = []
    while queue:
        name = queue.popleft()
        order.append(name)
        for child in children[name]:
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    if len(order) != len(by_name):
        cyclic = [n for n, d in indegree.items() if d > 0]
        raise GraphValidationError(f"graph contains a cycle involving {cyclic}")
    return order


def _check_single_output(layers: list[dict]) -> None:
    referenced = {l["input"] for l in layers if l["input"] != INPUT_NODE}
    sinks = [l["name"] for l in layers if l["name"] not in referenced]
    if len(sinks) != 1:
        raise GraphValidationError(
            f"graph must have exactly one output layer, found {sinks}"
        )


def _propagate_shapes(
    layers: list[dict], input_features: int, order: list[str] | None = None
) -> dict[str, int]:
    by_name = {l["name"]: l for l in layers}
    seq = order if order is not None else [l["name"] for l in layers]
    shapes: dict[str, int] = {}
    for name in seq:
        l = by_name[name]
        src = l["input"]
        in_dim = input_features if src == INPUT_NODE else shapes[src]
        if l["type"] == "dense":
            shapes[name] = l["out_features"]
        else:
            shapes[name] = in_dim
    return shapes


def spec_hash(spec: dict) -> str:
    """Stable content hash of a normalized spec."""
    import json

    blob = json.dumps(spec, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()


class DagModel(nn.Module):
    """Executes the layers in topological order, threading tensors by name."""

    def __init__(self, spec: dict):
        super().__init__()
        self.spec = spec
        self._order = _topological_order(spec["layers"])
        self._by_name = {l["name"]: l for l in spec["layers"]}
        shapes = _propagate_shapes(spec["layers"], spec["input_features"], self._order)

        modules: dict[str, nn.Module] = {}
        for name in self._order:
            layer = self._by_name[name]
            src = layer["input"]
            in_dim = (
                spec["input_features"]
                if src == INPUT_NODE
                else shapes[src]
            )
            if layer["type"] == "dense":
                modules[name] = nn.Linear(in_dim, layer["out_features"])
            elif layer["type"] == "relu":
                modules[name] = nn.ReLU()
            else:
                modules[name] = nn.Dropout(layer["p"])
        self.modules_dict = nn.ModuleDict(modules)
        referenced = {
            l["input"] for l in spec["layers"] if l["input"] != INPUT_NODE
        }
        self.output_name = next(
            l["name"] for l in spec["layers"] if l["name"] not in referenced
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        tensors: dict[str, torch.Tensor] = {}
        for name in self._order:
            layer = self._by_name[name]
            inp = x if layer["input"] == INPUT_NODE else tensors[layer["input"]]
            tensors[name] = self.modules_dict[name](inp)
        return tensors[self.output_name]


def build_model(spec: dict) -> DagModel:
    return DagModel(spec)


def output_dim(spec: dict) -> int:
    order = _topological_order(spec["layers"])
    shapes = _propagate_shapes(spec["layers"], spec["input_features"], order)
    referenced = {l["input"] for l in spec["layers"] if l["input"] != INPUT_NODE}
    sink = next(l["name"] for l in spec["layers"] if l["name"] not in referenced)
    return shapes[sink]
