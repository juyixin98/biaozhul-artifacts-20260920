"""检查点的保存、校验与加载。

格式：`<name>.ckpt.npz`（NumPy 数组 + JSON 元数据）+ `<name>.ckpt.sha256` 校验文件。
写入采用「临时文件 + 原子 rename」，避免进程在写盘中途被杀留下半个检查点。
加载时强制校验 SHA-256，任何字节损坏都会抛出 CorruptCheckpointError。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

import numpy as np

CHECKPOINT_VERSION = 1
CKPT_SUFFIX = ".ckpt.npz"
SHA_SUFFIX = ".ckpt.sha256"


class CorruptCheckpointError(Exception):
    """检查点缺失、校验和不匹配或内容无法解析。"""


def _sha256_of_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def save_checkpoint(state: dict, meta: dict, ckpt_dir: str | Path, step: int) -> Path:
    """把完整训练状态写入检查点，返回检查点路径。

    state: str -> np.ndarray / 标量，必须包含模型、优化器、RNG、数据游标全部状态。
    meta: 可 JSON 序列化的元信息（配置、版本、保存时间等）。
    """
    ckpt_dir = Path(ckpt_dir)
    ckpt_dir.mkdir(parents=True, exist_ok=True)

    meta = dict(meta)
    meta["checkpoint_version"] = CHECKPOINT_VERSION
    meta["step"] = int(step)
    payload = {k: np.asarray(v) for k, v in state.items()}
    payload["__meta_json__"] = np.frombuffer(
        json.dumps(meta, sort_keys=True).encode("utf-8"), dtype=np.uint8
    )

    ckpt_path = ckpt_dir / f"step_{step:08d}{CKPT_SUFFIX}"
    tmp_path = ckpt_dir / f"step_{step:08d}{CKPT_SUFFIX}.tmp"
    with open(tmp_path, "wb") as f:
        np.savez(f, **payload)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, ckpt_path)

    _atomic_write(
        ckpt_dir / f"step_{step:08d}{SHA_SUFFIX}",
        (_sha256_of_file(ckpt_path) + "\n").encode("ascii"),
    )
    return ckpt_path


def sha_path_for(ckpt_path: str | Path) -> Path:
    ckpt_path = Path(ckpt_path)
    return ckpt_path.with_name(ckpt_path.name.replace(CKPT_SUFFIX, SHA_SUFFIX))


def verify_checkpoint(ckpt_path: str | Path) -> None:
    """校验检查点完整性；任何异常都归一为 CorruptCheckpointError。"""
    ckpt_path = Path(ckpt_path)
    sha_path = sha_path_for(ckpt_path)
    if not ckpt_path.is_file():
        raise CorruptCheckpointError(f"检查点不存在: {ckpt_path}")
    if not sha_path.is_file():
        raise CorruptCheckpointError(f"缺少校验文件: {sha_path}")
    expected = sha_path.read_text(encoding="ascii").strip()
    actual = _sha256_of_file(ckpt_path)
    if actual != expected:
        raise CorruptCheckpointError(
            f"SHA-256 校验失败: {ckpt_path} (期望 {expected[:12]}…, 实际 {actual[:12]}…)"
        )


def load_checkpoint(ckpt_path: str | Path) -> tuple[dict, dict]:
    """校验并加载检查点，返回 (state, meta)。"""
    ckpt_path = Path(ckpt_path)
    verify_checkpoint(ckpt_path)
    try:
        with np.load(ckpt_path, allow_pickle=False) as data:
            state = {k: data[k] for k in data.files if k != "__meta_json__"}
            meta = json.loads(bytes(data["__meta_json__"].tolist()).decode("utf-8"))
    except CorruptCheckpointError:
        raise
    except Exception as exc:  # 校验和正确但内容无法解析，同样视为损坏
        raise CorruptCheckpointError(f"检查点无法解析: {ckpt_path}: {exc}") from exc
    if meta.get("checkpoint_version") != CHECKPOINT_VERSION:
        raise CorruptCheckpointError(
            f"不支持的检查点版本: {meta.get('checkpoint_version')}"
        )
    return state, meta


def list_checkpoints(ckpt_dir: str | Path) -> list[Path]:
    """按 step 升序列出目录下所有检查点文件。"""
    ckpt_dir = Path(ckpt_dir)
    if not ckpt_dir.is_dir():
        return []
    pattern = re.compile(r"^step_\d{8}" + re.escape(CKPT_SUFFIX) + r"$")
    return sorted(p for p in ckpt_dir.iterdir() if pattern.match(p.name))


def find_latest_valid(ckpt_dir: str | Path) -> Path | None:
    """从最新到最旧找到第一个通过完整性校验的检查点；全部损坏则返回 None。"""
    for path in reversed(list_checkpoints(ckpt_dir)):
        try:
            verify_checkpoint(path)
            return path
        except CorruptCheckpointError:
            continue
    return None


def prune_checkpoints(ckpt_dir: str | Path, keep_last: int) -> list[Path]:
    """只保留最近 keep_last 个检查点，返回被删除的路径。"""
    removed = []
    all_ckpts = list_checkpoints(ckpt_dir)
    for path in all_ckpts[: max(0, len(all_ckpts) - keep_last)]:
        sha_path = sha_path_for(path)
        path.unlink(missing_ok=True)
        sha_path.unlink(missing_ok=True)
        removed.append(path)
    return removed
