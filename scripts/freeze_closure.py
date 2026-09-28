#!/usr/bin/env python3
"""生成 requirements-lock.txt：递归解析直接依赖的传递闭包并固定实测版本。

用法：python3 scripts/freeze_closure.py > requirements-lock.txt
"""

from __future__ import annotations

import importlib.metadata as md

from packaging.requirements import Requirement

ROOTS = ["fastapi", "uvicorn", "pydantic", "httpx", "pytest", "pytest-cov"]


def closure() -> dict[str, str]:
    seen: set[str] = set()
    stack = list(ROOTS)
    while stack:
        name = stack.pop().lower()
        if name in seen:
            continue
        try:
            md.metadata(name)
        except md.PackageNotFoundError:
            continue
        seen.add(name)
        for req in md.requires(name) or []:
            r = Requirement(req)
            if r.marker is not None:
                try:
                    if not r.marker.evaluate():
                        continue
                except Exception:
                    continue
            stack.append(r.name)
    return {name: md.version(name) for name in sorted(seen)}


def main() -> None:
    print("# 自动生成：python3 scripts/freeze_closure.py")
    for name, version in closure().items():
        print(f"{name}=={version}")


if __name__ == "__main__":
    main()
