"""CycloneDX JSON 明确子集的解析与依赖图构建。

支持的子集：

* ``bomFormat == "CycloneDX"``，``specVersion`` 1.4 / 1.5 / 1.6。
* ``components[]``：字段 ``bom-ref`` / ``type`` / ``name`` / ``version`` /
  ``scope`` / ``purl``（顶层 purl）。
* ``dependencies[]``：``ref`` + ``dependsOn[]`` 的 bom-ref 邻接表。
* ``metadata.component``：作为直接（根）组件，scope 视为 ``required``。

组件身份（合并键）由规范化 purl 决定：

* 同一 purl（含同一版本）重复出现 -> 合并为一个节点；
* 不同版本或不同 purl qualifiers（变体，如 npm 的
  ``?os=linux`` / ``?os=darwin``）-> 不同节点，绝不混同；
* scope 冲突时 ``required`` 优先于 ``optional``；``excluded`` 不覆盖前者。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .purl import Purl, PurlError, parse_purl

SUPPORTED_SPEC_VERSIONS = {"1.4", "1.5", "1.6"}
_SCOPE_ORDER = {"excluded": 0, "optional": 1, "required": 2}


class SbomError(ValueError):
    """CycloneDX 文档不在支持子集内或结构非法。"""


@dataclass
class Component:
    ref: str
    purl: Purl
    bom_type: str
    scope: str  # required / optional / excluded
    name: str
    merged_from: list[str] = field(default_factory=list)
    is_root: bool = False
    direct: bool = False  # 是否为根组件的直接依赖

    @property
    def key(self) -> str:
        """合并键：规范化 purl（含版本与 qualifiers）。"""
        return self.purl.canonical(include_version=True)

    @property
    def version(self) -> str | None:
        return self.purl.version

    def to_dict(self) -> dict:
        return {
            "bom_ref": self.ref,
            "purl": self.purl.canonical(),
            "ecosystem": self.purl.ecosystem,
            "name": self.purl.name,
            "namespace": self.purl.namespace,
            "version": self.purl.version,
            "qualifiers": dict(self.purl.qualifiers),
            "bom_type": self.bom_type,
            "scope": self.scope,
            "direct": self.direct,
            "merged_duplicate_refs": list(self.merged_from),
            "is_root": self.is_root,
        }


@dataclass
class IgnoredComponent:
    ref: str
    name: str | None
    reason: str
    detail: str


@dataclass
class SbomReport:
    spec_version: str
    serial_number: str | None
    version: int | None
    components: list[Component]
    edges: list[tuple[str, str]]         # (from_key, to_key)
    root_keys: list[str]
    ignored: list[IgnoredComponent]
    cycles: list[list[str]]
    warnings: list[str]
    _by_key: dict[str, Component] = field(default_factory=dict)
    _key_of_ref: dict[str, str] = field(default_factory=dict)
    _adj: dict[str, set[str]] = field(default_factory=dict)

    def adjacency(self) -> dict[str, set[str]]:
        return self._adj

    def key_of_ref(self, ref: str) -> str | None:
        return self._key_of_ref.get(ref)


def _norm_scope(raw: str | None) -> str:
    if raw is None:
        return "required"
    value = raw.strip().lower()
    if value not in _SCOPE_ORDER:
        raise SbomError(f"unsupported component scope: {raw!r}")
    return value


def _merge_scope(existing: str, incoming: str) -> str:
    return existing if _SCOPE_ORDER[existing] >= _SCOPE_ORDER[incoming] else incoming


def _ingest_component(raw: dict, components: dict[str, Component],
                      ref_to_key: dict[str, str], ignored: list[IgnoredComponent],
                      *, is_root: bool) -> None:
    ref = raw.get("bom-ref") or raw.get("purl") or raw.get("name")
    if not isinstance(ref, str) or not ref:
        ignored.append(IgnoredComponent(
            ref="", name=raw.get("name"),
            reason="missing_bom_ref",
            detail="component has no bom-ref/purl/name and cannot be referenced"))
        return

    purl_text = raw.get("purl")
    if not purl_text:
        ignored.append(IgnoredComponent(
            ref=ref, name=raw.get("name"),
            reason="missing_purl",
            detail="component without top-level purl is outside the supported subset"))
        return

    try:
        purl = parse_purl(purl_text)
    except PurlError as exc:
        ignored.append(IgnoredComponent(
            ref=ref, name=raw.get("name"),
            reason="invalid_purl", detail=str(exc)))
        return

    if purl.version is None:
        ignored.append(IgnoredComponent(
            ref=ref, name=raw.get("name"),
            reason="missing_version",
            detail=f"purl {purl.canonical(include_version=False)} has no version; "
                   "kept out of range matching (results would be unknown)"))
        # 仍登记 ref -> 一个不可匹配键，保证依赖边能解析
        placeholder_key = f"unversioned::{purl.canonical(include_version=False)}"
        ref_to_key[ref] = placeholder_key
        return

    scope = _norm_scope(raw.get("scope"))
    if is_root and scope != "excluded":
        scope = "required"

    key = purl.canonical(include_version=True)
    existing = components.get(key)
    if existing is None:
        components[key] = Component(
            ref=ref, purl=purl,
            bom_type=str(raw.get("type") or "library"),
            scope=scope, name=str(raw.get("name") or purl.name),
            is_root=is_root)
    else:
        existing.merged_from.append(ref)
        existing.scope = _merge_scope(existing.scope, scope)
        existing.is_root = existing.is_root or is_root
    ref_to_key[ref] = key


def _find_cycles(adj: dict[str, set[str]]) -> list[list[str]]:
    """Tarjan 强连通分量；size>1（含自环）的 SCC 即循环。

    只遍历真实节点；边目标若不在节点集合（缺失版本/被忽略组件的占位
    键）则跳过，避免凭空产生假 SCC。
    """
    nodes = set(adj)
    index_of: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    counter = 0
    cycles: list[list[str]] = []

    def strongconnect(v: str) -> None:
        nonlocal counter
        index_of[v] = lowlink[v] = counter
        counter += 1
        stack.append(v)
        on_stack.add(v)
        for w in adj.get(v, ()):
            if w not in nodes:
                continue
            if w not in index_of:
                strongconnect(w)
                lowlink[v] = min(lowlink[v], lowlink[w])
            elif w in on_stack:
                lowlink[v] = min(lowlink[v], index_of[w])
        if lowlink[v] == index_of[v]:
            comp: list[str] = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                comp.append(w)
                if w == v:
                    break
            if len(comp) > 1 or v in adj.get(v, ()):
                cycles.append(sorted(comp))

    for node in sorted(nodes):
        if node not in index_of:
            strongconnect(node)
    cycles.sort()
    return cycles


def parse_cyclonedx(doc: dict) -> SbomReport:
    if not isinstance(doc, dict):
        raise SbomError("SBOM document must be a JSON object")
    if doc.get("bomFormat") != "CycloneDX":
        raise SbomError("bomFormat must be 'CycloneDX'")
    spec = str(doc.get("specVersion") or "")
    if spec not in SUPPORTED_SPEC_VERSIONS:
        raise SbomError(
            f"unsupported specVersion {spec!r}; supported: "
            f"{sorted(SUPPORTED_SPEC_VERSIONS)}")

    raw_components = doc.get("components") or []
    if not isinstance(raw_components, list):
        raise SbomError("components must be an array")
    raw_deps = doc.get("dependencies") or []
    if not isinstance(raw_deps, list):
        raise SbomError("dependencies must be an array")

    components: dict[str, Component] = {}
    ref_to_key: dict[str, str] = {}
    ignored: list[IgnoredComponent] = []

    root = doc.get("metadata", {}).get("component") if doc.get("metadata") else None
    if isinstance(root, dict):
        _ingest_component(root, components, ref_to_key, ignored, is_root=True)

    for raw in raw_components:
        if not isinstance(raw, dict):
            raise SbomError("each component must be an object")
        _ingest_component(raw, components, ref_to_key, ignored, is_root=False)

    warnings: list[str] = []
    edges: list[tuple[str, str]] = []
    adj: dict[str, set[str]] = {k: set() for k in components}
    for entry in raw_deps:
        if not isinstance(entry, dict) or "ref" not in entry:
            raise SbomError("each dependency entry needs a 'ref'")
        src_ref = entry["ref"]
        dependson = entry.get("dependsOn") or []
        if not isinstance(dependson, list):
            raise SbomError("dependsOn must be an array")
        src_key = ref_to_key.get(src_ref)
        if src_key is None:
            warnings.append(
                f"dependencies.ref {src_ref!r} does not match any component; skipped")
            continue
        adj.setdefault(src_key, set())
        for dst_ref in dependson:
            dst_key = ref_to_key.get(dst_ref)
            if dst_key is None:
                warnings.append(
                    f"dependency edge {src_ref!r} -> {dst_ref!r} targets an "
                    "unknown/unmatched component; skipped")
                continue
            if dst_key.startswith("unversioned::"):
                warnings.append(
                    f"dependency edge {src_ref!r} -> {dst_ref!r} targets a "
                    "component without version; edge kept but target not matched")
            if (src_key, dst_key) not in edges:
                edges.append((src_key, dst_key))
            adj.setdefault(dst_key, set())
            adj[src_key].add(dst_key)

    root_keys = [k for k, c in components.items() if c.is_root]
    # 直接依赖：被任一 root 直接引用的组件
    for rk in root_keys:
        for child in adj.get(rk, ()):
            if child in components and child not in root_keys:
                components[child].direct = True

    # 未出现在任何依赖表中的组件：若无 root，则整体视为直接组件
    referenced_as_dep = {dst for _, dst in edges}
    if root_keys:
        for k, c in components.items():
            if k not in root_keys and k not in referenced_as_dep:
                c.direct = True
                warnings.append(
                    f"component {k} is not connected to the dependency graph; "
                    "treated as a direct (top-level) component")
    else:
        warnings.append(
            "no metadata.component present; all components are treated as top-level")
        for c in components.values():
            c.direct = True

    cycles = _find_cycles(adj)

    return SbomReport(
        spec_version=spec,
        serial_number=doc.get("serialNumber"),
        version=doc.get("version") if isinstance(doc.get("version"), int) else None,
        components=list(components.values()),
        edges=edges,
        root_keys=root_keys,
        ignored=ignored,
        cycles=cycles,
        warnings=warnings,
        _by_key=components,
        _key_of_ref=ref_to_key,
        _adj=adj,
    )
