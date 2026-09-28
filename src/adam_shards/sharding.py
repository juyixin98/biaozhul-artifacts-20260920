"""Sharded checkpoint save / restore / reshard across local worker processes.

Layout
------
All parameters are flattened into one index space in *canonical name order*.
A checkpoint of ``world_size`` partitions [0, total) into ``world_size``
contiguous ranges; the first ``remainder`` ranks receive one extra element
(the tail shard is therefore uneven) and a parameter may straddle a boundary.

Save uses ``world_size`` spawned writer processes; restore uses a possibly
different ``world_size`` of reader processes (source shard files are handed
round-robin to the readers). Identity is always (name, shape); traversal order
never matters.

Commit model
------------
Model (params) and optimizer (m, v, per-parameter step) are written together
and published by a single atomic ``manifest.json`` replace. The manifest
carries a content digest per shard file and per part, so byte tampering,
shape tampering or step tampering is detected.
"""
from __future__ import annotations

import hashlib
import json
import logging
import multiprocessing as mp
import os
import tempfile
from dataclasses import dataclass

import numpy as np

from .errors import (
    CheckpointError,
    CommitMismatchError,
    CorruptManifestError,
    DigestMismatchError,
    IncompleteManifestError,
    MissingShardError,
    ParameterUnknownError,
    ShapeMismatchError,
)
from .tensor_types import PARAM, MOMENT1, MOMENT2, STORAGE_DTYPE

FORMAT_VERSION = 1
KINDS = (PARAM, MOMENT1, MOMENT2)
log = logging.getLogger("adam_shards.sharding")


@dataclass(frozen=True)
class ParamLayout:
    name: str
    shape: tuple[int, ...]
    offset: int
    numel: int
    step: int


@dataclass(frozen=True)
class FlatLayout:
    """Canonical flat index space over sorted parameter names."""

    params: tuple[ParamLayout, ...]
    total: int

    @classmethod
    def build(
        cls,
        shapes: dict[str, tuple[int, ...]],
        steps: dict[str, int] | None = None,
    ) -> "FlatLayout":
        offset = 0
        layouts = []
        for name in sorted(shapes):
            numel = int(np.prod(shapes[name]))
            layouts.append(
                ParamLayout(name, tuple(shapes[name]), offset, numel,
                            int((steps or {}).get(name, 0)))
            )
            offset += numel
        return cls(tuple(layouts), offset)

    def by_name(self) -> dict[str, ParamLayout]:
        return {p.name: p for p in self.params}

    def shapes(self) -> dict[str, tuple[int, ...]]:
        return {p.name: p.shape for p in self.params}

    def steps(self) -> dict[str, int]:
        return {p.name: p.step for p in self.params}


@dataclass(frozen=True)
class Slice:
    """A contiguous run of flat elements of one parameter on one shard."""

    name: str
    start: int  # global flat index, inclusive
    end: int    # global flat index, exclusive


def plan_ranges(total: int, world_size: int) -> list[tuple[int, int]]:
    """Contiguous balanced ranges; the first ``remainder`` ranks get one
    extra element, so the final range can be shorter (uneven tail)."""
    if world_size <= 0:
        raise ValueError("world_size must be positive")
    base, rem = divmod(total, world_size)
    ranges, cursor = [], 0
    for r in range(world_size):
        length = base + (1 if r < rem else 0)
        ranges.append((cursor, cursor + length))
        cursor += length
    return ranges


def slices_for_range(layout: FlatLayout, lo: int, hi: int) -> list[Slice]:
    """Intersect one rank range with every parameter (straddle-safe)."""
    out: list[Slice] = []
    for p in layout.params:
        s, e = max(lo, p.offset), min(hi, p.offset + p.numel)
        if s < e:
            out.append(Slice(p.name, s, e))
    return out


# --------------------------------------------------------------------------- #
# Multiprocessing workers (top-level so the spawn start method can pickle them)
# --------------------------------------------------------------------------- #
def _write_one_file(path: str, blob: bytes) -> tuple[str, int]:
    with open(path, "wb") as fh:
        fh.write(blob)
    return hashlib.sha256(blob).hexdigest(), len(blob)


def _writer_worker(task: dict) -> dict:
    rank = task["rank"]
    result = {"rank": rank, "pid": os.getpid(), "files": {}}
    for kind, entries in task["kinds"].items():
        # Concatenate this rank's per-parameter slices in canonical order.
        blob = b"".join(entry["bytes"] for entry in entries)
        path = os.path.join(task["dir"], task["filenames"][kind])
        digest, size = _write_one_file(path, blob)
        result["files"][kind] = {"digest": digest, "size": size}
    return result


def _reader_worker(task: dict) -> dict:
    """Verify digest of owned shard files, return their raw payloads."""
    out = {"pid": os.getpid(), "files": []}
    for f in task["files"]:
        path = os.path.join(task["dir"], f["file"])
        if not os.path.isfile(path):
            raise MissingShardError(
                f"shard file missing: rank={f['rank']} kind={f['kind']} path={path}"
            )
        with open(path, "rb") as fh:
            blob = fh.read()
        digest = hashlib.sha256(blob).hexdigest()
        if len(blob) != f["size"]:
            raise DigestMismatchError(
                f"shard size mismatch rank={f['rank']} kind={f['kind']}: "
                f"declared {f['size']} got {len(blob)}"
            )
        if digest != f["digest"]:
            raise DigestMismatchError(
                f"shard digest mismatch rank={f['rank']} kind={f['kind']} "
                f"declared {f['digest'][:12]} actual {digest[:12]}"
            )
        out["files"].append({"rank": f["rank"], "kind": f["kind"], "blob": blob})
    return out


def _spawn_pool(world_size: int) -> mp.pool.Pool:
    # Use "spawn" so workers are genuinely independent interpreters (safe even
    # when the parent is multi-threaded, e.g. the ASGI server). spawn children
    # inherit the parent process sys.path, so the in-tree package resolves when
    # tests/CLI run with src on PYTHONPATH (see pyproject pythonpath).
    ctx = mp.get_context("spawn")
    return ctx.Pool(processes=world_size)


# --------------------------------------------------------------------------- #
# Manifest construction
# --------------------------------------------------------------------------- #
def _shard_filename(rank: int, kind: str) -> str:
    return f"shard_{rank:05d}_{kind}.bin"


def _canonical_digest(payload: dict) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _part_digest(
    layout: FlatLayout,
    file_digests: dict[int, dict[str, str]],
    kinds: tuple[str, ...],
    include_steps: bool,
) -> str:
    payload = {
        "layout": [
            [p.name, list(p.shape), p.offset, p.numel] for p in layout.params
        ],
        "files": [
            [rank, kind, file_digests[rank][kind]]
            for rank in sorted(file_digests)
            for kind in kinds
        ],
    }
    if include_steps:
        payload["steps"] = {p.name: p.step for p in layout.params}
    return _canonical_digest(payload)


def _build_manifest(
    commit_id: str,
    layout: FlatLayout,
    world_size: int,
    slice_plan: list[list[Slice]],
    writer_results: list[dict],
) -> dict:
    file_digests = {r["rank"]: {k: v["digest"] for k, v in r["files"].items()}
                    for r in writer_results}
    params_section = {
        p.name: {"shape": list(p.shape), "offset": p.offset,
                 "numel": p.numel, "step": p.step}
        for p in layout.params
    }
    shards = []
    for rank in range(world_size):
        shards.append({
            "rank": rank,
            "writer_pid": writer_results[rank]["pid"],
            "files": {
                kind: {
                    "file": _shard_filename(rank, kind),
                    "digest": file_digests[rank][kind],
                    "size": writer_results[rank]["files"][kind]["size"],
                }
                for kind in KINDS
            },
            "slices": [
                {"param": s.name, "start": s.start, "end": s.end}
                for s in slice_plan[rank]
            ],
        })
    model_digest = _part_digest(layout, file_digests, (PARAM,), False)
    optim_digest = _part_digest(layout, file_digests, (MOMENT1, MOMENT2), True)
    return {
        "format_version": FORMAT_VERSION,
        "commit_id": commit_id,
        "world_size": world_size,
        "total_elements": layout.total,
        "param_order": [p.name for p in layout.params],
        "params": params_section,
        "shards": shards,
        "parts": {
            "model": {"commit_id": commit_id, "digest": model_digest},
            "optimizer": {"commit_id": commit_id, "digest": optim_digest},
        },
    }


# --------------------------------------------------------------------------- #
# Save
# --------------------------------------------------------------------------- #
def save_checkpoint(
    ckpt_dir: str,
    param_arrays: dict[str, np.ndarray],
    moments: dict[str, "tuple[np.ndarray, np.ndarray, int]"],
    world_size: int,
    *,
    commit_id: str | None = None,
    request_id: str | None = None,
    logger: logging.LoggerAdapter | logging.Logger | None = None,
) -> str:
    """Atomically publish one model+optimizer commit under ``ckpt_dir``."""
    lg = logger or log
    shapes = {n: a.shape for n, a in param_arrays.items()}
    if set(moments) != set(param_arrays):
        raise KeyError("moments and params name sets differ")
    steps = {n: int(moments[n][2]) for n in moments}
    layout = FlatLayout.build(shapes, steps)
    if world_size > layout.total:
        raise ValueError("world_size cannot exceed total number of elements")
    os.makedirs(ckpt_dir, exist_ok=True)

    ranges = plan_ranges(layout.total, world_size)
    slice_plan = [slices_for_range(layout, lo, hi) for lo, hi in ranges]
    flats = {
        PARAM: {n: np.ascontiguousarray(param_arrays[n]).reshape(-1) for n in shapes},
        MOMENT1: {n: np.ascontiguousarray(moments[n][0]).reshape(-1) for n in shapes},
        MOMENT2: {n: np.ascontiguousarray(moments[n][1]).reshape(-1) for n in shapes},
    }

    tasks = []
    for rank, slices in enumerate(slice_plan):
        kinds_payload: dict[str, list[dict]] = {k: [] for k in KINDS}
        for kind in KINDS:
            for s in slices:
                p = layout.by_name()[s.name]
                lo, hi = s.start - p.offset, s.end - p.offset
                kinds_payload[kind].append({
                    "bytes": flats[kind][s.name][lo:hi].tobytes(order="C")
                })
        tasks.append({
            "rank": rank, "dir": ckpt_dir,
            "filenames": {k: _shard_filename(rank, k) for k in KINDS},
            "kinds": kinds_payload,
        })

    lg.info("save commit start world_size=%d total=%d ranges=%s",
            world_size, layout.total, ranges)
    with _spawn_pool(world_size) as pool:
        results = pool.map(_writer_worker, tasks)
    results.sort(key=lambda r: r["rank"])
    for r in results:
        lg.info("writer pid=%s rank=%d wrote %d kinds",
                r["pid"], r["rank"], len(r["files"]))

    commit_id = commit_id or _commit_id(layout, results)
    manifest = _build_manifest(commit_id, layout, world_size, slice_plan, results)
    _publish_manifest(ckpt_dir, manifest)
    lg.info("save committed commit_id=%s model_digest=%s optim_digest=%s",
            commit_id,
            manifest["parts"]["model"]["digest"][:12],
            manifest["parts"]["optimizer"]["digest"][:12])
    return commit_id


def _commit_id(layout: FlatLayout, results: list[dict]) -> str:
    h = hashlib.sha256()
    h.update(str(layout.total).encode())
    for p in layout.params:
        h.update(p.name.encode())
        h.update(str(p.shape).encode())
        h.update(str(p.step).encode())
    for r in sorted(results, key=lambda x: x["rank"]):
        for kind in KINDS:
            h.update(r["files"][kind]["digest"].encode())
    return h.hexdigest()[:16]


def _publish_manifest(ckpt_dir: str, manifest: dict) -> None:
    """Atomic commit: manifest appears once all shard files are durable."""
    final = os.path.join(ckpt_dir, "manifest.json")
    fd, tmp = tempfile.mkstemp(prefix=".manifest-", suffix=".tmp", dir=ckpt_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, final)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


# --------------------------------------------------------------------------- #
# Manifest validation
# --------------------------------------------------------------------------- #
def read_manifest(ckpt_dir: str) -> dict:
    path = os.path.join(ckpt_dir, "manifest.json")
    if not os.path.isfile(path):
        raise MissingShardError(f"manifest not found under {ckpt_dir}")
    try:
        with open(path, encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise CorruptManifestError(f"manifest is not valid JSON: {exc}") from exc
    if not isinstance(manifest, dict):
        raise CorruptManifestError("manifest root must be an object")
    return manifest


def _validate_structure(manifest: dict) -> None:
    required = {"format_version", "commit_id", "world_size", "total_elements",
                "param_order", "params", "shards", "parts"}
    missing = required - set(manifest)
    if missing:
        raise CorruptManifestError(f"manifest missing keys: {sorted(missing)}")
    if manifest["format_version"] != FORMAT_VERSION:
        raise CorruptManifestError(
            f"unsupported format_version {manifest['format_version']}"
        )
    parts = manifest["parts"]
    if not isinstance(parts, dict) or "model" not in parts or "optimizer" not in parts:
        raise CommitMismatchError(
            "commit must contain both model and optimizer parts"
        )
    top = manifest["commit_id"]
    for part_name, part in parts.items():
        if part.get("commit_id") != top:
            raise CommitMismatchError(
                f"part {part_name!r} commit_id={part.get('commit_id')} does not "
                f"match commit {top}: model and optimizer were not saved together"
            )
    if not isinstance(manifest["params"], dict) or not manifest["params"]:
        raise CorruptManifestError("manifest params section invalid")
    if sorted(manifest["param_order"]) != sorted(manifest["params"]):
        raise CorruptManifestError("param_order and params disagree")


def _layout_from_manifest(manifest: dict) -> FlatLayout:
    try:
        entries = []
        cursor = 0
        for name in manifest["param_order"]:
            spec = manifest["params"][name]
            shape = tuple(int(d) for d in spec["shape"])
            offset, numel = int(spec["offset"]), int(spec["numel"])
            step = int(spec["step"])
            if offset != cursor:
                raise IncompleteManifestError(
                    f"parameter layout not contiguous at {name!r}: "
                    f"offset {offset} != {cursor}"
                )
            if numel != int(np.prod(shape)) or numel <= 0 or step < 0:
                raise CorruptManifestError(f"invalid layout entry for {name!r}")
            entries.append(ParamLayout(name, shape, offset, numel, step))
            cursor += numel
        if cursor != int(manifest["total_elements"]):
            raise IncompleteManifestError(
                f"layout covers {cursor} elements but manifest declares "
                f"{manifest['total_elements']}"
            )
        return FlatLayout(tuple(entries), cursor)
    except KeyError as exc:
        raise CorruptManifestError(f"manifest layout entry malformed: {exc}") from exc


def _validate_coverage(manifest: dict, layout: FlatLayout) -> None:
    """Union of declared slices must exactly cover [0, total) per kind."""
    ws = int(manifest["world_size"])
    shard_ranks = sorted(int(s["rank"]) for s in manifest["shards"])
    if shard_ranks != list(range(ws)):
        raise IncompleteManifestError(
            f"shard ranks {shard_ranks} do not form 0..{ws - 1}"
        )
    by_name = layout.by_name()
    for kind in KINDS:
        covered = np.zeros(layout.total, dtype=np.int8)
        for shard in manifest["shards"]:
            for s in shard["slices"]:
                if s["param"] not in by_name:
                    raise CorruptManifestError(
                        f"slice references unknown param {s['param']!r}"
                    )
                p = by_name[s["param"]]
                lo, hi = int(s["start"]), int(s["end"])
                if not (p.offset <= lo < hi <= p.offset + p.numel):
                    raise IncompleteManifestError(
                        f"slice {s['param']}[{lo},{hi}) outside parameter bounds"
                    )
                if covered[lo:hi].any():
                    raise IncompleteManifestError(
                        f"overlapping {kind} coverage at [{lo},{hi})"
                    )
                covered[lo:hi] = 1
        if not covered.all():
            gap = int(np.argmin(covered))
            raise IncompleteManifestError(
                f"{kind} coverage incomplete: gap starts at flat index {gap} "
                f"of {layout.total}"
            )


def _validate_part_digests(manifest: dict, layout: FlatLayout,
                           file_digests: dict[int, dict[str, str]]) -> None:
    expect_model = _part_digest(layout, file_digests, (PARAM,), False)
    expect_optim = _part_digest(layout, file_digests, (MOMENT1, MOMENT2), True)
    actual_model = manifest["parts"]["model"]["digest"]
    actual_optim = manifest["parts"]["optimizer"]["digest"]
    if actual_model != expect_model:
        raise DigestMismatchError(
            "model summary digest mismatch (layout or param shards altered "
            "after commit)"
        )
    if actual_optim != expect_optim:
        raise DigestMismatchError(
            "optimizer summary digest mismatch (moments, steps or layout "
            "altered after commit)"
        )


# --------------------------------------------------------------------------- #
# Restore
# --------------------------------------------------------------------------- #
def load_flat_checkpoint(
    ckpt_dir: str,
    world_size: int,
    *,
    request_id: str | None = None,
    logger: logging.LoggerAdapter | logging.Logger | None = None,
) -> tuple[dict, FlatLayout, dict[int, dict[str, bytes]]]:
    """Read & verify a checkpoint using ``world_size`` reader processes.

    Returns (manifest, layout, payloads) where payloads[rank][kind] is the
    verified raw blob of one source shard.
    """
    lg = logger or log
    try:
        return _load_flat_checkpoint_checked(
            ckpt_dir, world_size, request_id=request_id, logger=lg
        )
    except CheckpointError as exc:
        exc.request_id = request_id
        raise


def _load_flat_checkpoint_checked(
    ckpt_dir: str,
    world_size: int,
    *,
    request_id: str | None,
    logger: logging.LoggerAdapter | logging.Logger,
) -> tuple[dict, FlatLayout, dict[int, dict[str, bytes]]]:
    manifest = read_manifest(ckpt_dir)
    _validate_structure(manifest)
    layout = _layout_from_manifest(manifest)
    _validate_coverage(manifest, layout)

    files = []
    for shard in manifest["shards"]:
        rank = int(shard["rank"])
        for kind in KINDS:
            meta = shard["files"][kind]
            files.append({
                "rank": rank, "kind": kind, "file": meta["file"],
                "digest": meta["digest"], "size": int(meta["size"]),
            })
    # Round-robin assignment so world_size may differ from source shard count.
    assignments: list[list[dict]] = [[] for _ in range(world_size)]
    for i, f in enumerate(sorted(files, key=lambda x: (x["rank"], x["kind"]))):
        assignments[i % world_size].append(f)

    logger.info("restore start source_world=%d reader_world=%d total=%d",
                manifest["world_size"], world_size, layout.total)
    tasks = [{"dir": ckpt_dir, "files": batch}
             for batch in assignments if batch]
    with _spawn_pool(len(tasks)) as pool:
        worker_outs = pool.map(_reader_worker, tasks)

    payloads: dict[int, dict[str, bytes]] = {}
    file_digests: dict[int, dict[str, str]] = {}
    pids = []
    for out in worker_outs:
        pids.append(out["pid"])
        for f in out["files"]:
            payloads.setdefault(f["rank"], {})[f["kind"]] = f["blob"]
            file_digests.setdefault(f["rank"], {})[f["kind"]] = hashlib.sha256(
                f["blob"]).hexdigest()
    _validate_part_digests(manifest, layout, file_digests)
    logger.info("restore verified via reader pids=%s commit=%s",
                pids, manifest["commit_id"])
    return manifest, layout, payloads


def _assemble_flat(layout: FlatLayout, manifest: dict,
                   payloads: dict[int, dict[str, bytes]]) -> dict[str, np.ndarray]:
    flats = {kind: np.empty(layout.total, dtype=STORAGE_DTYPE) for kind in KINDS}
    for shard in manifest["shards"]:
        rank = int(shard["rank"])
        for kind in KINDS:
            blob = payloads[rank][kind]
            cursor = 0
            for s in shard["slices"]:
                length = int(s["end"]) - int(s["start"])
                nbytes = length * STORAGE_DTYPE.itemsize
                flats[kind][s["start"]:s["end"]] = np.frombuffer(
                    blob[cursor:cursor + nbytes], dtype=STORAGE_DTYPE
                )
                cursor += nbytes
            if cursor != len(blob):
                raise DigestMismatchError(
                    f"rank={rank} kind={kind} blob has {len(blob)} bytes, "
                    f"slices consume {cursor}"
                )
    return flats


def restore_arrays(
    ckpt_dir: str,
    world_size: int,
    target_shapes: dict[str, tuple[int, ...]] | None = None,
    *,
    request_id: str | None = None,
    logger: logging.LoggerAdapter | logging.Logger | None = None,
) -> dict:
    """Restore a checkpoint, reshaping against ``target_shapes``.

    ``target_shapes`` is the restore-side graph identity. Names absent there
    raise :class:`ParameterUnknownError`; differing shapes raise
    :class:`ShapeMismatchError`.
    """
    manifest, layout, payloads = load_flat_checkpoint(
        ckpt_dir, world_size, request_id=request_id, logger=logger
    )
    if target_shapes is not None:
        ckpt_names = set(layout.by_name())
        target_names = set(target_shapes)
        extra = sorted(target_names - ckpt_names)
        missing = sorted(ckpt_names - target_names)
        if extra or missing:
            raise ParameterUnknownError(
                f"parameter identity set differs from checkpoint; "
                f"only_in_restore_graph={extra} only_in_checkpoint={missing}"
            )
        for p in layout.params:
            want = tuple(target_shapes[p.name])
            if want != p.shape:
                raise ShapeMismatchError(
                    f"parameter {p.name!r}: checkpoint shape {p.shape} vs "
                    f"restore graph shape {want}"
                )
    flats = _assemble_flat(layout, manifest, payloads)
    by_name = layout.by_name()
    params, moments = {}, {}
    for name in sorted(by_name):
        p = by_name[name]
        seg = lambda kind: flats[kind][p.offset:p.offset + p.numel].reshape(p.shape)  # noqa: E731
        params[name] = np.array(seg(PARAM), copy=True)
        moments[name] = (
            np.array(seg(MOMENT1), copy=True),
            np.array(seg(MOMENT2), copy=True),
            p.step,
        )
    return {
        "commit_id": manifest["commit_id"],
        "params": params,
        "moments": moments,
        "shapes": layout.shapes(),
        "steps": layout.steps(),
        "source_world_size": manifest["world_size"],
    }


# --------------------------------------------------------------------------- #
# Reshard to a different process count
# --------------------------------------------------------------------------- #
def reshard_checkpoint(
    src_dir: str,
    dst_dir: str,
    new_world_size: int,
    *,
    request_id: str | None = None,
    logger: logging.LoggerAdapter | logging.Logger | None = None,
) -> str:
    """Read a checkpoint and publish it under a new shard count/commit."""
    lg = logger or log
    loaded = restore_arrays(src_dir, new_world_size,
                            request_id=request_id, logger=lg)
    new_commit = save_checkpoint(
        dst_dir,
        loaded["params"],
        loaded["moments"],
        new_world_size,
        request_id=request_id,
        logger=lg,
    )
    lg.info("resharded %d procs -> %d procs new_commit=%s",
            loaded["source_world_size"], new_world_size, new_commit)
    return new_commit
