"""Loading of Foundry build artifacts (``out/<File>.sol/<Contract>.json``)."""

from __future__ import annotations

import json
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parent.parent / "out"


class ArtifactNotFound(KeyError):
    pass


def _artifact_files(out_dir: Path) -> list[Path]:
    if not out_dir.is_dir():
        return []
    return [
        p
        for p in out_dir.glob("*/*.json")
        if p.parent.name != "build-info" and not p.name.endswith(".metadata.json")
    ]


def load_artifacts(out_dir: Path | str = OUT_DIR) -> dict[str, dict]:
    """Return ``{contract_name: artifact}`` for every compiled contract."""
    out_dir = Path(out_dir)
    artifacts: dict[str, dict] = {}
    for path in sorted(_artifact_files(out_dir)):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        name = path.stem  # forge names artifacts after the contract
        artifacts[name] = data
    return artifacts


def get_artifact(name: str, out_dir: Path | str = OUT_DIR) -> dict:
    artifacts = load_artifacts(out_dir)
    if name not in artifacts:
        raise ArtifactNotFound(
            f"contract {name!r} not found in {out_dir}; run `forge build` first. "
            f"Available: {', '.join(sorted(artifacts)) or '(none)'}"
        )
    return artifacts[name]


def get_layout(name: str, out_dir: Path | str = OUT_DIR) -> dict:
    artifact = get_artifact(name, out_dir)
    layout = artifact.get("storageLayout")
    if layout is None:
        raise ArtifactNotFound(
            f"artifact for {name!r} has no storageLayout; "
            'rebuild with extra_output = ["storageLayout"] in foundry.toml'
        )
    return layout


def get_base_contracts(name: str, out_dir: Path | str = OUT_DIR) -> list[str]:
    """Ancestor contract names of ``name`` in linearized (C3) order.

    Read from ``out/bases.json``, which is produced at build time by
    ``scripts/export_bases.py`` (solc's layout JSON always names the
    most-derived contract, so bases cannot be recovered from it).
    Returns [] when the file or entry is missing.
    """
    bases_file = Path(out_dir) / "bases.json"
    try:
        data = json.loads(bases_file.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    return list(data.get(name, []))
