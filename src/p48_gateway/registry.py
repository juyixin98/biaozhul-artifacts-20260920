"""Robot/tester registry: robot_id -> namespace mapping and tester keys/ACLs.

The registry file is JSON (see ``config/registry.example.json``).  An
``epoch`` sidecar file (``<registry>.epoch``) stores the current mapping
version so that restarting the gateway cannot roll the epoch back: the loaded
epoch is always ``max(persisted, recorded mapping epochs)``.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .crypto import b64decode
from .topics import validate_namespace, validate_robot_id


@dataclass(frozen=True)
class Tester:
    name: str
    key: str
    robots: frozenset[str]  # robot ids this tester may command ("*" = all)


@dataclass(frozen=True)
class Robot:
    robot_id: str
    namespace: str


@dataclass
class Registry:
    admin_key: str
    default_ttl: float
    epoch_key: str
    testers: dict[str, Tester] = field(default_factory=dict)
    robots: dict[str, Robot] = field(default_factory=dict)

    @classmethod
    def from_data(cls, data: dict[str, Any]) -> "Registry":
        if not isinstance(data, dict):
            raise ValueError("registry root must be an object")
        admin_key = data.get("admin_key")
        if not isinstance(admin_key, str) or len(admin_key) < 16:
            raise ValueError("admin_key must be a string of >=16 chars")
        epoch_key = data.get("epoch_key")
        if not isinstance(epoch_key, str):
            raise ValueError("epoch_key missing")
        b64decode(epoch_key)  # validates 32-byte key
        default_ttl = float(data.get("default_ttl_seconds", 5.0))
        if not 0.0 < default_ttl <= 3600.0:
            raise ValueError("default_ttl_seconds must be within (0, 3600]")

        testers: dict[str, Tester] = {}
        for name, raw in (data.get("testers") or {}).items():
            validate_robot_id(name)
            if not isinstance(raw, dict):
                raise ValueError(f"tester {name!r} must be an object")
            key = raw.get("key")
            if not isinstance(key, str):
                raise ValueError(f"tester {name!r}: key missing")
            b64decode(key)
            robots = raw.get("robots", [])
            if not isinstance(robots, list) or not all(isinstance(r, str) for r in robots):
                raise ValueError(f"tester {name!r}: robots must be a list of ids")
            if "*" in robots and len(robots) != 1:
                raise ValueError(f"tester {name!r}: '*' must be the only ACL entry")
            testers[name] = Tester(name=name, key=key, robots=frozenset(robots))

        robots: dict[str, Robot] = {}
        seen_namespaces: set[str] = set()
        for rid, raw in (data.get("robots") or {}).items():
            validate_robot_id(rid)
            if not isinstance(raw, dict):
                raise ValueError(f"robot {rid!r} must be an object")
            namespace = validate_namespace(raw.get("namespace"))
            if namespace in seen_namespaces:
                raise ValueError(f"namespace {namespace!r} registered twice")
            # A namespace may not nest inside another one: "/p48/a" and
            # "/p48/a/cmd" would put one robot's topic tree *inside* the
            # other robot's namespace, breaking containment boundaries.
            for other in seen_namespaces:
                if namespace.startswith(other + "/") or other.startswith(namespace + "/"):
                    raise ValueError(
                        f"namespace {namespace!r} nests inside/around {other!r}"
                    )
            seen_namespaces.add(namespace)
            robots[rid] = Robot(robot_id=rid, namespace=namespace)

        for tester in testers.values():
            if "*" not in tester.robots:
                for rid in tester.robots:
                    if rid not in robots:
                        raise ValueError(f"tester {tester.name!r} ACLs unknown robot {rid!r}")

        return cls(
            admin_key=admin_key,
            default_ttl=default_ttl,
            epoch_key=epoch_key,
            testers=testers,
            robots=robots,
        )

    def tester_can_access(self, tester: Tester, robot_id: str) -> bool:
        return "*" in tester.robots or robot_id in tester.robots


def load_registry(path: str | Path) -> Registry:
    with open(path, "r", encoding="utf-8") as fh:
        return Registry.from_data(json.load(fh))


def read_persisted_epoch(path: str | Path) -> int:
    sidecar = Path(str(path) + ".epoch")
    try:
        text = sidecar.read_text(encoding="ascii").strip()
        value = int(text)
        return value if value >= 1 else 1
    except (FileNotFoundError, ValueError):
        return 1


def write_persisted_epoch(path: str | Path, epoch: int) -> None:
    sidecar = Path(str(path) + ".epoch")
    sidecar.write_text(str(int(epoch)), encoding="ascii")


class RegistryStore:
    """Thread-safe holder of the live registry and mapping epoch."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = threading.RLock()
        registry = load_registry(self._path)
        self._registry = registry
        self._epoch = max(1, read_persisted_epoch(self._path))
        self._mapping = self._mapping_signature(registry)
        write_persisted_epoch(self._path, self._epoch)

    @staticmethod
    def _mapping_signature(registry: Registry) -> dict[str, str]:
        return {rid: robot.namespace for rid, robot in sorted(registry.robots.items())}

    @property
    def path(self) -> Path:
        return self._path

    def snapshot(self) -> tuple[Registry, int, dict[str, str]]:
        with self._lock:
            return self._registry, self._epoch, dict(self._mapping)

    def reload(self) -> tuple[Registry, int, bool]:
        """Reload the file; bump+persist epoch iff the id->ns mapping changed."""
        new_registry = load_registry(self._path)
        new_mapping = self._mapping_signature(new_registry)
        with self._lock:
            changed = new_mapping != self._mapping
            self._registry = new_registry
            self._mapping = new_mapping
            if changed:
                self._epoch += 1
                write_persisted_epoch(self._path, self._epoch)
            return self._registry, self._epoch, changed
