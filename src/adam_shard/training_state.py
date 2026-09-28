"""Checkpoint commits: atomic save, strict validation and re-sharding load.

On-disk layout for one commit::

    <root>/<commit_id>/manifest.json
    <root>/<commit_id>/model-r{rank}.npy
    <root>/<commit_id>/optim-r{rank}.npz   (arrays "m" and "v")

Model parameters and optimizer moments are published together in one
atomic directory rename under a single ``commit_id``; a loader never accepts
a model/optimizer pair drawn from different commits.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np

from .adam import AdamConfig
from .errors import (
    CheckpointError,
    CommitMismatchError,
    CorruptManifestError,
    DigestMismatchError,
    IncompleteManifestError,
    MissingCommitError,
    MissingShardError,
    ShapeMismatchError,
    UnsupportedVersionError,
)
from .layout import ParamLayout
from .sharding import ShardPlan, reshard_indices
from .tensor_types import RAW_DTYPE, array_digest, json_digest

FORMAT_VERSION = 1
_MODEL_FILE = "model-r{rank}.npy"
_OPTIM_FILE = "optim-r{rank}.npz"


# --------------------------------------------------------------------------- save


def _stage_path(root: str, commit_id: str) -> str:
    return os.path.join(root, f".stage-{commit_id}")


def _commit_path(root: str, commit_id: str) -> str:
    return os.path.join(root, commit_id)


def save_commit(
    root: str,
    commit_id: str,
    layout: ParamLayout,
    plan: ShardPlan,
    step: int,
    adam_cfg: AdamConfig,
    param_flat: np.ndarray,
    m_flat: np.ndarray,
    v_flat: np.ndarray,
) -> dict:
    """Write a complete model+optimizer commit and publish it atomically."""

    if not commit_id or "/" in commit_id or commit_id in (".", ".."):
        raise ValueError("commit_id must be a non-empty single path segment")
    if step < 0:
        raise ValueError("step must be non-negative")
    param_flat = np.ascontiguousarray(param_flat, dtype=RAW_DTYPE).reshape(-1)
    m_flat = np.ascontiguousarray(m_flat, dtype=RAW_DTYPE).reshape(-1)
    v_flat = np.ascontiguousarray(v_flat, dtype=RAW_DTYPE).reshape(-1)
    if not (param_flat.shape == m_flat.shape == v_flat.shape == (plan.total_numel,)):
        raise ShapeMismatchError(
            f"flat state size mismatch: param {param_flat.shape}, m {m_flat.shape}, "
            f"v {v_flat.shape}, plan {plan.total_numel}",
            commit_id=commit_id,
        )

    os.makedirs(root, exist_ok=True)
    stage = _stage_path(root, commit_id)
    final = _commit_path(root, commit_id)
    if os.path.exists(final):
        raise FileExistsError(f"commit {commit_id!r} already exists at {final}")
    if os.path.exists(stage):  # leftover from a crashed publish
        _rmtree(stage)
    os.makedirs(stage)

    model_entries: list[dict] = []
    optim_entries: list[dict] = []
    try:
        for rank, (start, end) in enumerate(plan.boundaries):
            p_slice = param_flat[start:end]
            m_slice = m_flat[start:end]
            v_slice = v_flat[start:end]

            m_name, o_name = _MODEL_FILE.format(rank=rank), _OPTIM_FILE.format(rank=rank)
            np.save(os.path.join(stage, m_name), p_slice)
            np.savez(os.path.join(stage, o_name), m=m_slice, v=v_slice)

            model_entries.append(
                {
                    "rank": rank,
                    "path": m_name,
                    "start": start,
                    "end": end,
                    "numel": end - start,
                    "digest": array_digest(p_slice),
                }
            )
            optim_entries.append(
                {
                    "rank": rank,
                    "path": o_name,
                    "start": start,
                    "end": end,
                    "numel": end - start,
                    "m_digest": array_digest(m_slice),
                    "v_digest": array_digest(v_slice),
                }
            )

        body = {
            "version": FORMAT_VERSION,
            "commit_id": commit_id,
            "world_size": plan.world_size,
            "step": step,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "adam": adam_cfg.to_dict(),
            "layout": layout.to_manifest(),
            "plan": plan.to_manifest(),
            "model_shards": model_entries,
            "optim_shards": optim_entries,
        }
        body["manifest_digest"] = json_digest({k: v for k, v in body.items() if k != "manifest_digest"})
        _atomic_write_json(os.path.join(stage, "manifest.json"), body)
        os.rename(stage, final)  # atomic publish on a single filesystem
    except BaseException:
        _rmtree(stage)
        raise

    _atomic_write_text(os.path.join(root, "LATEST"), commit_id + "\n")
    return body


def _atomic_write_json(path: str, payload: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _atomic_write_text(path: str, text: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def _rmtree(path: str) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)


# --------------------------------------------------------------------------- load


@dataclass(frozen=True)
class ShardSlice:
    rank: int
    start: int
    end: int
    param: np.ndarray
    m: np.ndarray
    v: np.ndarray

    @property
    def numel(self) -> int:
        return self.end - self.start


@dataclass(frozen=True)
class LoadedCommit:
    commit_id: str
    step: int
    adam_cfg: AdamConfig
    layout: ParamLayout
    source_plan: ShardPlan
    target_plan: ShardPlan
    param_flat: np.ndarray
    m_flat: np.ndarray
    v_flat: np.ndarray
    shards: tuple[ShardSlice, ...]

    @property
    def world_size(self) -> int:
        return self.target_plan.world_size


def list_commits(root: str) -> list[str]:
    if not os.path.isdir(root):
        return []
    out = []
    for name in sorted(os.listdir(root)):
        if not name.startswith(".") and name != "LATEST" and os.path.isfile(
            os.path.join(root, name, "manifest.json")
        ):
            out.append(name)
    return out


def resolve_latest(root: str) -> str:
    pointer = os.path.join(root, "LATEST")
    if not os.path.isfile(pointer):
        raise MissingCommitError("no LATEST pointer and no commit id given")
    commit_id = open(pointer, encoding="utf-8").read().strip()
    if not commit_id or not os.path.isdir(_commit_path(root, commit_id)):
        raise MissingCommitError(f"LATEST points at missing commit {commit_id!r}")
    return commit_id


def _read_manifest(root: str, commit_id: str) -> dict:
    cdir = _commit_path(root, commit_id)
    manifest_path = os.path.join(cdir, "manifest.json")
    if not os.path.isfile(manifest_path):
        raise MissingCommitError(f"commit {commit_id!r} not found under {root}", commit_id=commit_id)
    try:
        with open(manifest_path, encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        raise CorruptManifestError(
            f"manifest for {commit_id!r} is not readable JSON: {exc}", commit_id=commit_id
        ) from exc

    recorded = manifest.get("manifest_digest")
    body = {k: v for k, v in manifest.items() if k != "manifest_digest"}
    if not isinstance(recorded, str) or json_digest(body) != recorded:
        raise CorruptManifestError(
            "manifest summary digest mismatch; manifest was altered after commit",
            commit_id=commit_id,
        )
    if manifest.get("version") != FORMAT_VERSION:
        raise UnsupportedVersionError(
            f"checkpoint version {manifest.get('version')!r} unsupported; want {FORMAT_VERSION}",
            commit_id=commit_id,
        )
    return manifest


def _require_complete_entries(manifest: dict, key: str, world_size: int, commit_id: str) -> list[dict]:
    entries = manifest.get(key)
    if not isinstance(entries, list) or len(entries) != world_size:
        raise IncompleteManifestError(
            f"manifest {key!r} incomplete: declared world_size={world_size}, "
            f"listed {len(entries) if isinstance(entries, list) else 'non-list'}",
            commit_id=commit_id,
        )
    ranks = sorted(e.get("rank") for e in entries if isinstance(e, dict))
    if ranks != list(range(world_size)):
        raise IncompleteManifestError(
            f"manifest {key!r} ranks {ranks} are not exactly 0..{world_size - 1}",
            commit_id=commit_id,
        )
    return sorted(entries, key=lambda e: e["rank"])


def _assemble(
    root: str,
    commit_id: str,
    manifest: dict,
    plan: ShardPlan,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    cdir = _commit_path(root, commit_id)
    model_entries = _require_complete_entries(manifest, "model_shards", plan.world_size, commit_id)
    optim_entries = _require_complete_entries(manifest, "optim_shards", plan.world_size, commit_id)

    total = plan.total_numel
    param_flat = np.empty(total, dtype=RAW_DTYPE)
    m_flat = np.empty(total, dtype=RAW_DTYPE)
    v_flat = np.empty(total, dtype=RAW_DTYPE)

    for m_entry, o_entry in zip(model_entries, optim_entries):
        rank = int(m_entry["rank"])
        start, end = plan.span(rank)

        m_path = os.path.join(cdir, str(m_entry["path"]))
        o_path = os.path.join(cdir, str(o_entry["path"]))
        if not os.path.isfile(m_path) or not os.path.isfile(o_path):
            raise MissingShardError(
                f"rank {rank} shard file missing: {m_entry['path']!r} or {o_entry['path']!r}",
                commit_id=commit_id,
                detail={"rank": rank},
            )

        p = np.load(m_path)
        with np.load(o_path) as zf:
            m_arr, v_arr = zf["m"].copy(), zf["v"].copy()

        for name, arr, expect_numel in (
            ("model", p, end - start),
            ("m", m_arr, end - start),
            ("v", v_arr, end - start),
        ):
            if arr.ndim != 1 or arr.shape[0] != expect_numel:
                raise ShapeMismatchError(
                    f"{name} shard rank {rank} has shape {arr.shape}, want ({expect_numel},)",
                    commit_id=commit_id,
                    detail={"rank": rank, "got": list(arr.shape), "want": [expect_numel]},
                )

        p = arr_as_raw(p)
        m_arr = arr_as_raw(m_arr)
        v_arr = arr_as_raw(v_arr)
        if array_digest(p) != m_entry["digest"]:
            raise DigestMismatchError(
                f"model shard rank {rank} digest mismatch; content is corrupt or swapped",
                commit_id=commit_id,
                detail={"rank": rank, "tensor": "model"},
            )
        if array_digest(m_arr) != o_entry["m_digest"]:
            raise DigestMismatchError(
                f"first-moment shard rank {rank} digest mismatch; content is corrupt or swapped",
                commit_id=commit_id,
                detail={"rank": rank, "tensor": "m"},
            )
        if array_digest(v_arr) != o_entry["v_digest"]:
            raise DigestMismatchError(
                f"second-moment shard rank {rank} digest mismatch; content is corrupt or swapped",
                commit_id=commit_id,
                detail={"rank": rank, "tensor": "v"},
            )

        param_flat[start:end] = p
        m_flat[start:end] = m_arr
        v_flat[start:end] = v_arr

    return param_flat, m_flat, v_flat


def arr_as_raw(arr: np.ndarray) -> np.ndarray:
    return np.ascontiguousarray(arr, dtype=RAW_DTYPE).reshape(-1)


def load_commit(root: str, commit_id: str | None = None, target_world_size: int | None = None) -> LoadedCommit:
    """Validate and load a commit, re-sharding to ``target_world_size``."""

    commit_id = commit_id or resolve_latest(root)
    if "/" in commit_id or commit_id in (".", ".."):
        raise CheckpointError("illegal commit id", commit_id=commit_id)
    manifest, layout, plan = _parse_verified_manifest(root, commit_id)

    source_world = plan.world_size
    step = int(manifest["step"])
    adam_cfg = AdamConfig(**{k: float(v) for k, v in manifest["adam"].items()})
    param_flat, m_flat, v_flat = _assemble(root, commit_id, manifest, plan)

    target_world = target_world_size or source_world
    target_plan = ShardPlan.create(layout.total_numel, target_world)
    segments = reshard_indices(plan, target_plan)
    shards: list[ShardSlice] = []
    for d_rank, (d_start, d_end) in enumerate(target_plan.boundaries):
        p_dst = np.empty(d_end - d_start, dtype=RAW_DTYPE)
        m_dst = np.empty_like(p_dst)
        v_dst = np.empty_like(p_dst)
        for s_rank, s_off, d_off, length in segments[d_rank]:
            s_start, _ = plan.span(s_rank)
            # Source reassembly is global, so map local offsets back to globals.
            p_dst[d_off : d_off + length] = param_flat[s_start + s_off : s_start + s_off + length]
            m_dst[d_off : d_off + length] = m_flat[s_start + s_off : s_start + s_off + length]
            v_dst[d_off : d_off + length] = v_flat[s_start + s_off : s_start + s_off + length]
        shards.append(ShardSlice(d_rank, d_start, d_end, p_dst, m_dst, v_dst))

    return LoadedCommit(
        commit_id=commit_id,
        step=step,
        adam_cfg=adam_cfg,
        layout=layout,
        source_plan=plan,
        target_plan=target_plan,
        param_flat=param_flat,
        m_flat=m_flat,
        v_flat=v_flat,
        shards=tuple(shards),
    )


@dataclass(frozen=True)
class RankView:
    """What a single restoring rank needs: only its target flat slice."""

    rank: int
    start: int
    end: int
    step: int
    param: np.ndarray
    m: np.ndarray
    v: np.ndarray


def _parse_verified_manifest(root: str, commit_id: str) -> tuple[dict, ParamLayout, ShardPlan]:
    """Verify manifest summary, identity, completeness and plan; parse pieces."""

    manifest = _read_manifest(root, commit_id)
    if manifest.get("commit_id") != commit_id:
        raise CommitMismatchError(
            f"directory named {commit_id!r} but manifest claims {manifest.get('commit_id')!r}",
            commit_id=commit_id,
        )
    layout = ParamLayout.from_manifest(manifest["layout"])
    source_world = int(manifest["world_size"])
    plan = ShardPlan.create(layout.total_numel, source_world)

    model_entries = _require_complete_entries(manifest, "model_shards", source_world, commit_id)
    optim_entries = _require_complete_entries(manifest, "optim_shards", source_world, commit_id)
    for entries, key in ((model_entries, "model_shards"), (optim_entries, "optim_shards")):
        for entry in entries:
            rank = int(entry["rank"])
            start, end = plan.span(rank)
            if int(entry["start"]) != start or int(entry["end"]) != end:
                raise IncompleteManifestError(
                    f"{key} rank {rank} span ({entry.get('start')},{entry.get('end')}) "
                    f"disagrees with canonical plan ({start},{end})",
                    commit_id=commit_id,
                )

    declared = [(int(r["start"]), int(r["end"])) for r in sorted(manifest["plan"], key=lambda r: r["rank"])]
    if declared != list(plan.boundaries):
        raise IncompleteManifestError(
            "stored shard plan does not match the canonical uneven-tail plan",
            commit_id=commit_id,
        )
    return manifest, layout, plan


def load_rank_view(root: str, commit_id: str, target_world_size: int, rank: int) -> RankView:
    """Load one rank's slice after re-sharding, touching only needed source shards."""

    manifest, layout, src_plan = _parse_verified_manifest(root, commit_id)
    dst_plan = ShardPlan.create(layout.total_numel, target_world_size)
    if not 0 <= rank < target_world_size:
        raise IndexError(f"rank {rank} out of range for world_size {target_world_size}")
    d_start, d_end = dst_plan.span(rank)
    segments = reshard_indices(src_plan, dst_plan)[rank]

    p_dst = np.empty(d_end - d_start, dtype=RAW_DTYPE)
    m_dst = np.empty_like(p_dst)
    v_dst = np.empty_like(p_dst)
    cdir = _commit_path(root, commit_id)
    entries_by_rank = {int(e["rank"]): e for e in manifest["model_shards"]}, {
        int(e["rank"]): e for e in manifest["optim_shards"]
    }
    model_by_rank, optim_by_rank = entries_by_rank
    for s_rank, s_off, d_off, length in segments:
        m_entry, o_entry = model_by_rank[s_rank], optim_by_rank[s_rank]
        m_path, o_path = os.path.join(cdir, str(m_entry["path"])), os.path.join(cdir, str(o_entry["path"]))
        if not os.path.isfile(m_path) or not os.path.isfile(o_path):
            raise MissingShardError(
                f"rank {rank} needs source rank {s_rank} whose shard file is missing",
                commit_id=commit_id,
                detail={"rank": rank, "source_rank": s_rank},
            )
        p = arr_as_raw(np.load(m_path))
        with np.load(o_path) as zf:
            m_arr, v_arr = arr_as_raw(zf["m"]), arr_as_raw(zf["v"])
        s_start, s_end = src_plan.span(s_rank)
        for name, arr in (("model", p), ("m", m_arr), ("v", v_arr)):
            if arr.shape != (s_end - s_start,):
                raise ShapeMismatchError(
                    f"{name} shard rank {s_rank} has shape {arr.shape}, want ({s_end - s_start},)",
                    commit_id=commit_id,
                    detail={"source_rank": s_rank, "tensor": name},
                )
        if array_digest(p) != m_entry["digest"]:
            raise DigestMismatchError(
                f"model shard rank {s_rank} digest mismatch",
                commit_id=commit_id,
                detail={"source_rank": s_rank, "tensor": "model"},
            )
        if array_digest(m_arr) != o_entry["m_digest"] or array_digest(v_arr) != o_entry["v_digest"]:
            raise DigestMismatchError(
                f"moment shard rank {s_rank} digest mismatch",
                commit_id=commit_id,
                detail={"source_rank": s_rank, "tensor": "m/v"},
            )
        p_dst[d_off : d_off + length] = p[s_off : s_off + length]
        m_dst[d_off : d_off + length] = m_arr[s_off : s_off + length]
        v_dst[d_off : d_off + length] = v_arr[s_off : s_off + length]

    return RankView(
        rank=rank, start=d_start, end=d_end, step=int(manifest["step"]),
        param=p_dst, m=m_dst, v=v_dst,
    )


def require_same_commit(model_commit: str, optim_commit: str) -> None:
    """Refuse pairing a model checkpoint and optimizer state from different commits."""

    if model_commit != optim_commit:
        raise CommitMismatchError(
            f"model commit {model_commit!r} and optimizer commit {optim_commit!r} differ; "
            "model and optimizer must be restored from one atomic commit",
            commit_id=model_commit,
            detail={"model_commit": model_commit, "optim_commit": optim_commit},
        )
