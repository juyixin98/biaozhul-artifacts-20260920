"""Static tensor computation DAG with known-shape add / matmul / slice nodes.

A DAG is built from a plain dict (the request format)::

    {
      "inputs":  {"a": [2, 3], ...},      # name -> shape
      "ops": [
        {"name": "t1", "op": "matmul", "inputs": ["a", "w"]},
        {"name": "v1", "op": "slice", "inputs": ["t1"],
         "starts": [0, 0], "stops": [2, 2]},
        {"name": "t2", "op": "add", "inputs": ["v1", "b"]},
      ],
      "outputs": ["t2"]
    }

Slice semantics are NumPy basic-indexing style with per-axis `start:stop`
ranges (steps other than 1 are rejected): the result is a *view* into the
source storage, which the memory planner must treat as an alias.
"""

from __future__ import annotations

from dataclasses import dataclass, field

ADD = "add"
MATMUL = "matmul"
SLICE = "slice"

SUPPORTED_OPS = frozenset({ADD, MATMUL, SLICE})

# Resource guards: keep a single crafted request from allocating/executing
# absurd tensors (e.g. [1,100000] + [100000,1] broadcasts to 1e10 elements).
MAX_DIMENSION = 1_000_000
MAX_TENSOR_ELEMENTS = 100_000_000  # 400 MiB at float32
MAX_OPS = 10_000


class DAGValidationError(ValueError):
    """Raised when a request does not describe a valid, supported DAG."""


def _check_size(shape: tuple[int, ...], what: str) -> None:
    for axis, dim in enumerate(shape):
        if dim > MAX_DIMENSION:
            raise DAGValidationError(
                f"{what}: dimension {dim} on axis {axis} exceeds the "
                f"supported maximum {MAX_DIMENSION}"
            )
    elements = 1
    for dim in shape:
        elements *= dim
    if elements > MAX_TENSOR_ELEMENTS:
        raise DAGValidationError(
            f"{what}: tensor has {elements} elements, exceeding the supported "
            f"maximum {MAX_TENSOR_ELEMENTS}"
        )


@dataclass(frozen=True)
class SliceSpec:
    """Per-axis half-open ``[start, stop)`` ranges; step is always 1."""

    starts: tuple[int, ...]
    stops: tuple[int, ...]

    def output_shape(self, src_shape: tuple[int, ...]) -> tuple[int, ...]:
        if len(self.starts) != len(src_shape):
            raise DAGValidationError(
                f"slice has {len(self.starts)} axis ranges but source tensor "
                f"has {len(src_shape)} axes"
            )
        out = []
        for axis, (dim, start, stop) in enumerate(
            zip(src_shape, self.starts, self.stops)
        ):
            if not (0 <= start <= stop <= dim):
                raise DAGValidationError(
                    f"slice range [{start}:{stop}] out of bounds for axis {axis} "
                    f"of size {dim}"
                )
            out.append(stop - start)
        return tuple(out)


@dataclass(frozen=True)
class Node:
    """One DAG node: an input variable or an op result (SSA-style)."""

    name: str
    op: str                      # "input" | ADD | MATMUL | SLICE
    inputs: tuple[str, ...]
    shape: tuple[int, ...]
    slice_spec: SliceSpec | None = None
    is_input: bool = False
    is_output: bool = False


@dataclass
class DAG:
    """Validated DAG. ``order`` is a topological (here: definition) order."""

    nodes: dict[str, Node]
    order: list[str]
    inputs: list[str]
    outputs: list[str]
    # consumer name -> list of (consumer op node name, argument index)
    consumers: dict[str, list[tuple[str, int]]] = field(default_factory=dict)

    def shapes_known(self) -> bool:
        return all(dim > 0 for n in self.order for dim in self.nodes[n].shape)


def _as_shape(value: object, what: str) -> tuple[int, ...]:
    if not isinstance(value, (list, tuple)) or not value:
        raise DAGValidationError(f"{what} must be a non-empty list of dimensions")
    shape = []
    for i, dim in enumerate(value):
        if not isinstance(dim, int) or isinstance(dim, bool) or dim <= 0:
            raise DAGValidationError(
                f"{what}[{i}] = {dim!r} is not a positive integer dimension"
            )
        shape.append(dim)
    result = tuple(shape)
    _check_size(result, what)
    return result


def _broadcast_shape(
    a: tuple[int, ...], b: tuple[int, ...], ctx: str
) -> tuple[int, ...]:
    """NumPy broadcasting for add. Leading dimensions are aligned to the right."""
    out: list[int] = []
    for da, db in zip(a[::-1], b[::-1]):
        if da == db:
            out.append(da)
        elif da == 1:
            out.append(db)
        elif db == 1:
            out.append(da)
        else:
            raise DAGValidationError(
                f"{ctx}: shapes {a} and {b} are not broadcast-compatible"
            )
    longer = a if len(a) > len(b) else b
    prefix_len = abs(len(a) - len(b))
    prefix = list(longer[:prefix_len])
    return tuple(prefix + out[::-1])


def _matmul_shape(
    a: tuple[int, ...], b: tuple[int, ...], ctx: str
) -> tuple[int, ...]:
    """Shape rule for ``@``: 1-D/2-D core plus batched (>=2-D) broadcasting."""
    if len(a) < 1 or len(b) < 1:
        raise DAGValidationError(f"{ctx}: matmul requires at least 1-D operands")
    if len(a) == 1 and len(b) == 1:
        if a[0] != b[0]:
            raise DAGValidationError(
                f"{ctx}: vector matmul contraction mismatch {a} x {b}"
            )
        return ()
    if len(a) == 1:
        if a[0] != b[-2]:
            raise DAGValidationError(
                f"{ctx}: matmul contraction mismatch {a} x {b}"
            )
        return b[:-2] + (b[-1],)
    if len(b) == 1:
        if a[-1] != b[0]:
            raise DAGValidationError(
                f"{ctx}: matmul contraction mismatch {a} x {b}"
            )
        return a[:-1]
    if a[-1] != b[-2]:
        raise DAGValidationError(
            f"{ctx}: matmul contraction mismatch {a[-1]} != {b[-2]} "
            f"(shapes {a} x {b})"
        )
    batch = _broadcast_shape(a[:-2], b[:-2], ctx + " batch dims")
    return batch + (a[-2], b[-1])


def _build_slice_spec(op_node: object) -> SliceSpec:
    if not isinstance(op_node, dict):
        raise DAGValidationError("each op entry must be an object")
    starts = op_node.get("starts")
    stops = op_node.get("stops")
    if not isinstance(starts, list) or not isinstance(stops, list):
        raise DAGValidationError(
            "slice op requires integer-list 'starts' and 'stops'"
        )
    if len(starts) != len(stops):
        raise DAGValidationError("slice 'starts' and 'stops' length differ")
    s_out, e_out = [], []
    for i, (s, e) in enumerate(zip(starts, stops)):
        if not isinstance(s, int) or isinstance(s, bool):
            raise DAGValidationError(f"slice starts[{i}] = {s!r} not an int")
        if not isinstance(e, int) or isinstance(e, bool):
            raise DAGValidationError(f"slice stops[{i}] = {e!r} not an int")
        s_out.append(s)
        e_out.append(e)
    return SliceSpec(tuple(s_out), tuple(e_out))


def build_dag(request: dict) -> DAG:
    """Validate ``request`` and build a :class:`DAG` with inferred shapes."""
    if not isinstance(request, dict):
        raise DAGValidationError("request must be an object")
    raw_inputs = request.get("inputs", {})
    raw_ops = request.get("ops", [])
    raw_outputs = request.get("outputs", [])

    if not isinstance(raw_inputs, dict) or not raw_inputs:
        raise DAGValidationError("'inputs' must be a non-empty object name->shape")
    if not isinstance(raw_ops, list):
        raise DAGValidationError("'ops' must be a list")
    if len(raw_ops) > MAX_OPS:
        raise DAGValidationError(
            f"too many ops: {len(raw_ops)} > supported maximum {MAX_OPS}"
        )
    if not isinstance(raw_outputs, list) or not raw_outputs:
        raise DAGValidationError("'outputs' must be a non-empty list of names")

    nodes: dict[str, Node] = {}
    order: list[str] = []
    input_names: list[str] = []

    for name, raw_shape in raw_inputs.items():
        if name in nodes:
            raise DAGValidationError(f"duplicate input name {name!r}")
        shape = _as_shape(raw_shape, f"input {name!r}")
        nodes[name] = Node(
            name=name, op="input", inputs=(), shape=shape, is_input=True
        )
        order.append(name)
        input_names.append(name)

    for index, raw in enumerate(raw_ops):
        if not isinstance(raw, dict):
            raise DAGValidationError(f"ops[{index}] must be an object")
        name = raw.get("name")
        op = raw.get("op")
        arg_names = raw.get("inputs")
        if not isinstance(name, str) or not name:
            raise DAGValidationError(f"ops[{index}] requires a string 'name'")
        if name in nodes:
            raise DAGValidationError(f"ops[{index}]: name {name!r} already defined")
        if op not in SUPPORTED_OPS:
            raise DAGValidationError(
                f"ops[{index}] {name!r}: unsupported op {op!r}; "
                f"supported: {sorted(SUPPORTED_OPS)}"
            )
        if not isinstance(arg_names, list) or not arg_names:
            raise DAGValidationError(
                f"ops[{index}] {name!r}: 'inputs' must be a non-empty list"
            )

        arg_shapes: list[tuple[int, ...]] = []
        for arg in arg_names:
            if not isinstance(arg, str) or arg not in nodes:
                raise DAGValidationError(
                    f"ops[{index}] {name!r}: unknown input {arg!r}"
                )
            arg_shapes.append(nodes[arg].shape)

        spec: SliceSpec | None = None
        if op == ADD:
            if len(arg_names) != 2:
                raise DAGValidationError(
                    f"add {name!r} takes exactly 2 inputs, got {len(arg_names)}"
                )
            shape = _broadcast_shape(arg_shapes[0], arg_shapes[1], f"add {name!r}")
        elif op == MATMUL:
            if len(arg_names) != 2:
                raise DAGValidationError(
                    f"matmul {name!r} takes exactly 2 inputs, got {len(arg_names)}"
                )
            shape = _matmul_shape(arg_shapes[0], arg_shapes[1], f"matmul {name!r}")
        else:  # SLICE
            if len(arg_names) != 1:
                raise DAGValidationError(
                    f"slice {name!r} takes exactly 1 input, got {len(arg_names)}"
                )
            spec = _build_slice_spec(raw)
            shape = spec.output_shape(arg_shapes[0])

        _check_size(shape, f"{op} {name!r}")

        nodes[name] = Node(
            name=name,
            op=op,
            inputs=tuple(arg_names),
            shape=shape,
            slice_spec=spec,
        )
        order.append(name)

    outputs: list[str] = []
    for out in raw_outputs:
        if not isinstance(out, str) or out not in nodes:
            raise DAGValidationError(f"unknown output {out!r}")
        if out in outputs:
            raise DAGValidationError(f"duplicate output {out!r}")
        outputs.append(out)

    # Mark outputs (frozen dataclass -> replace) and build consumer index.
    for out in outputs:
        n = nodes[out]
        nodes[out] = Node(**{**n.__dict__, "is_output": True})

    consumers: dict[str, list[tuple[str, int]]] = {n: [] for n in order}
    for name in order:
        for i, arg in enumerate(nodes[name].inputs):
            consumers[arg].append((name, i))

    return DAG(
        nodes=nodes,
        order=order,
        inputs=input_names,
        outputs=outputs,
        consumers=consumers,
    )
