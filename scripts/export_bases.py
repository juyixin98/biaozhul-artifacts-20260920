#!/usr/bin/env python3
"""Export each compiled contract's linearized base contracts to out/bases.json.

Run after `forge build`. Uses `forge inspect <Contract> linearization --json`
because solc's storage-layout JSON always names the most-derived contract,
so inheritance information cannot be recovered from the layout itself.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "out"

sys.path.insert(0, str(ROOT))
from backend.artifacts import load_artifacts  # noqa: E402


def main() -> None:
    forge = shutil.which("forge") or str(Path.home() / ".foundry/bin/forge")
    bases: dict[str, list[str]] = {}
    for name in load_artifacts(OUT_DIR):
        result = subprocess.run(
            [forge, "inspect", name, "linearization", "--json"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        linearized = [entry["contract"] for entry in json.loads(result.stdout)]
        bases[name] = [c for c in linearized if c != name]
    out_file = OUT_DIR / "bases.json"
    out_file.write_text(json.dumps(bases, indent=2) + "\n")
    print(f"wrote {out_file} ({len(bases)} contracts)")


if __name__ == "__main__":
    main()
