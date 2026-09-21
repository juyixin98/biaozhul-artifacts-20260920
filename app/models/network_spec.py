"""Neural network specification: parsing, validation and torch construction.

A network is a JSON object::

    {
      "layers": [
        {"id": "in",  "type": "input",   "in_features": 4},
        {"id": "h1",  "type": "dense",  "out_features": 16},
        {"id": "a1",  "type": "relu"},
        {"id": "d1",  "type": "dropout", "p": 0.2},
        {"id": "out", "type": "dense",  "out_features": 3}
      ],
      "connections": [["in", "h1"], ["h1", "a1"], ["a1", "d1"], ["d1", "out"]]
    }

Only CPU Dense (Linear), ReLU and Dropout layers are supported. Validation
checks the graph is acyclic, shapes line up at every connection, and all
parameters are inside their allowed ranges.
"""
from __future__ import annotations

import hashlib
import json
from collections import deque
from dataclasses import dataclass
from typing import Any

import torch
from torch import nn

SUPPORTED_TYPES = {"input", "dense", "relu", "dropout"}

# Parameter ranges.
MAX_DENSE_FEATURES = 4096
MAX_INPUT_FEATURES = 65536
MAX_LAYERS = 128
DROPOUT_RANGE = (0.0, 0.9)  # p == 1.0 would zero every unit; reject it.


class SpecError(ValueError):
    """Raised when a network specification fails validation."""


@dataclass(frozen=True)
class Layer:
    id: str
    type: str
    in_features: int | None
    out_features: int | None
    dropout_p: float | None


@dataclass(frozen=True)
class NetworkSpec:
    layers: dict[str, Layer]
    order: list[str]
    edges: list[tuple[str, str]]
    input_layer: str
    output_layer: str
    in_features: int
    out_features: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "layers": [
                {k: v for k, v in {
                    "id": lid,
                    "type": self.layers[lid].type,
                    "in_features": self.layers[lid].in_features,
                    "out_features": self.layers[lid].out_features,
                    "p": self.layers[lid].dropout_p,
                }.items() if v is not None}
                for lid in self.order
            ],
            "connections": [list(e) for e in self.edges],
        }

    def fingerprint(self) -> str:
        """Stable SHA-256 over the canonicalised spec."""
        blob = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _require_int(obj: dict, key: str, layer_id: str, lo: int, hi: int) -> int:
    if key not in obj:
        raise SpecError(f"layer {layer_id!r}: missing required parameter {key!r}")
    val = obj[key]
    if isinstance(val, bool) or not isinstance(val, int):
        raise SpecError(f"layer {layer_id!r}: {key!r} must be an integer")
    if not lo <= val <= hi:
        raise SpecError(f"layer {layer_id!r}: {key}={val} out of range [{lo}, {hi}]")
    return val


def parse_spec(raw: dict[str, Any]) -> NetworkSpec:
    if not isinstance(raw, dict):
        raise SpecError("spec must be a JSON object")
    raw_layers = raw.get("layers")
    raw_edges = raw.get("connections", [])
    if not isinstance(raw_layers, list) or not raw_layers:
        raise SpecError("'layers' must be a non-empty list")
    if not isinstance(raw_edges, list):
        raise SpecError("'connections' must be a list")
    if len(raw_layers) > MAX_LAYERS:
        raise SpecError(f"too many layers ({len(raw_layers)} > {MAX_LAYERS})")

    layers: dict[str, Layer] = {}
    for obj in raw_layers:
        if not isinstance(obj, dict):
            raise SpecError("each layer must be an object")
        lid = obj.get("id")
        if not isinstance(lid, str) or not lid:
            raise SpecError("layer id must be a non-empty string")
        if lid in layers:
            raise SpecError(f"duplicate layer id {lid!r}")
        ltype = obj.get("type")
        if ltype not in SUPPORTED_TYPES:
            raise SpecError(
                f"layer {lid!r}: unsupported type {ltype!r}; "
                f"allowed: {sorted(SUPPORTED_TYPES)}"
            )
        in_f = out_f = None
        p = None
        if ltype == "input":
            in_f = out_f = _require_int(
                obj, "in_features", lid, 1, MAX_INPUT_FEATURES
            )
        elif ltype == "dense":
            out_f = _require_int(obj, "out_features", lid, 1, MAX_DENSE_FEATURES)
        elif ltype == "dropout":
            if "p" not in obj:
                raise SpecError(f"layer {lid!r}: dropout requires 'p'")
            p = obj["p"]
            if isinstance(p, bool) or not isinstance(p, (int, float)):
                raise SpecError(f"layer {lid!r}: 'p' must be a number")
            p = float(p)
            lo, hi = DROPOUT_RANGE
            if not lo <= p <= hi:
                raise SpecError(f"layer {lid!r}: p={p} out of range [{lo}, {hi}]")
        layers[lid] = Layer(id=lid, type=ltype, in_features=in_f,
                            out_features=out_f, dropout_p=p)

    edges: list[tuple[str, str]] = []
    adj: dict[str, list[str]] = {lid: [] for lid in layers}
    indegree: dict[str, int] = {lid: 0 for lid in layers}
    seen_edges: set[tuple[str, str]] = set()
    for edge in raw_edges:
        if not (isinstance(edge, list) and len(edge) == 2):
            raise SpecError(f"bad connection {edge!r}; expected [src, dst]")
        src, dst = edge
        if src not in layers or dst not in layers:
            raise SpecError(f"connection {edge!r} references unknown layer")
        pair = (src, dst)
        if pair in seen_edges:
            raise SpecError(f"duplicate connection {edge!r}")
        seen_edges.add(pair)
        edges.append(pair)
        adj[src].append(dst)
        indegree[dst] += 1

    inputs = [lid for lid, l in layers.items() if l.type == "input"]
    if len(inputs) != 1:
        raise SpecError(f"exactly one input layer required, found {len(inputs)}")
    input_layer = inputs[0]
    if indegree[input_layer] != 0:
        raise SpecError("input layer must not have incoming connections")

    # Kahn topological sort (rejects cycles) and shape propagation.
    queue = deque(sorted(lid for lid in layers if indegree[lid] == 0))
    order: list[str] = []
    indeg = dict(indegree)
    shapes: dict[str, int] = {}
    incoming_shape: dict[str, int | None] = {lid: None for lid in layers}
    while queue:
        lid = queue.popleft()
        layer = layers[lid]
        # Determine this layer's input shape.
        if layer.type == "input":
            shapes[lid] = layer.in_features  # type: ignore[assignment]
        else:
            src_shape = incoming_shape[lid]
            if src_shape is None:
                raise SpecError(f"layer {lid!r} has no incoming connection")
            if layer.type == "dense":
                shapes[lid] = layer.out_features  # type: ignore[assignment]
                layer = Layer(layer.id, layer.type, src_shape,
                              layer.out_features, layer.dropout_p)
                layers[lid] = layer
            elif layer.type == "relu":
                shapes[lid] = src_shape
                layers[lid] = Layer(lid, layer.type, src_shape, src_shape, None)
            else:  # dropout
                shapes[lid] = src_shape
                layers[lid] = Layer(lid, layer.type, src_shape, src_shape,
                                    layer.dropout_p)
        order.append(lid)
        for dst in sorted(adj[lid]):
            known = incoming_shape[dst]
            if known is not None and known != shapes[lid]:
                raise SpecError(
                    f"shape mismatch on inputs to {dst!r}: "
                    f"{known} vs {shapes[lid]}"
                )
            incoming_shape[dst] = shapes[lid]
            indeg[dst] -= 1
            if indeg[dst] == 0:
                queue.append(dst)

    if len(order) != len(layers):
        cyc = [lid for lid in layers if indeg[lid] > 0]
        raise SpecError(f"graph contains a cycle involving layers: {sorted(cyc)}")

    unreachable = [lid for lid in layers if lid not in order]
    if unreachable:
        raise SpecError(f"unreachable layers: {unreachable}")

    # Every non-input layer must have an incoming edge; every layer that
    # produces no output edge (other than the sole output) is forbidden.
    terminals = [lid for lid in layers if not adj[lid]]
    if len(terminals) != 1:
        raise SpecError(
            f"exactly one terminal (output) layer required, found {terminals}"
        )
    output_layer = terminals[0]
    missing_in = [
        lid for lid in layers
        if lid != input_layer and not any(dst == lid for _, dst in edges)
    ]
    if missing_in:
        raise SpecError(f"layers without incoming connections: {missing_in}")
    if layers[output_layer].type == "input":
        raise SpecError("network needs at least one layer after the input")
    if layers[output_layer].type == "dropout":
        raise SpecError("output layer may not be dropout")

    return NetworkSpec(
        layers=layers,
        order=order,
        edges=edges,
        input_layer=input_layer,
        output_layer=output_layer,
        in_features=layers[input_layer].in_features,  # type: ignore[arg-type]
        out_features=shapes[output_layer],
    )


def build_module(spec: NetworkSpec) -> nn.Module:
    """Construct the CPU torch module from a validated spec."""
    modules: list[nn.Module] = []
    for lid in spec.order:
        layer = spec.layers[lid]
        if layer.type == "input":
            continue  # identity; tensor shape is enforced by the dataset
        if layer.type == "dense":
            modules.append(nn.Linear(layer.in_features, layer.out_features))  # type: ignore[arg-type]
        elif layer.type == "relu":
            modules.append(nn.ReLU())
        else:
            modules.append(nn.Dropout(layer.dropout_p))  # type: ignore[arg-type]
    model = nn.Sequential(*modules)
    model.to(torch.device("cpu"))
    return model
