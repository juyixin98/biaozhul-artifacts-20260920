"""CycloneDX JSON parser — explicit, documented subset.

Accepted subset
---------------
* ``bomFormat`` must equal ``CycloneDX``; ``specVersion`` is required.
* ``components[]`` entries use ``type``, ``bom-ref``, ``name``, ``version``,
  ``purl`` and ``scope`` (``required`` / ``optional`` / ``excluded``;
  missing scope normalizes to ``required``).
* ``dependencies[]`` entries ``{ "ref": ..., "dependsOn": [...] }`` build the
  directed dependency graph. Unknown refs are reported as warnings, never
  dropped silently. Cycles are preserved.
* ``metadata.component.bom-ref`` identifies the root component; components
  directly referenced from it are direct dependencies.

Components are normalized by **(purl identity, version, scope)**: repeated
components merge their bom-refs, but different purl qualifiers/subpath are
different *variants* and are never merged.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .purl import Purl, PurlError, parse_purl

_VALID_SCOPES = {"required", "optional", "excluded"}
_VALID_TYPES = {
    "application", "framework", "library", "container",
    "operating-system", "device", "device-driver", "firmware",
    "file", "machine-learning-model", "data", "cryptographic-asset",
}


class CycloneDXError(ValueError):
    """The document is not acceptable CycloneDX for this subset."""


@dataclass
class Node:
    """A normalized component (one merge-group)."""
    key: tuple
    component_type: str
    cdx_name: str
    scope: str
    purl: Purl | None = None
    purl_error: str | None = None
    version: str | None = None
    bom_refs: set[str] = field(default_factory=set)
    raw_purls: list[str] = field(default_factory=list)
    merged_count: int = 1

    def display_purl(self) -> str | None:
        if self.purl is not None:
            return self.purl.canonical()
        return self.raw_purls[0] if self.raw_purls else None


@dataclass
class CycloneDX:
    spec_version: str
    document_ref: str | None
    root_ref: str | None
    nodes: dict[tuple, Node]
    ref_to_key: dict[str, tuple]
    edges: dict[str, set[str]]      # ref -> set(ref)
    metadata: dict
    warnings: list[str]


def _require(obj, key: str, ctx: str, expected_type):
    if key not in obj:
        raise CycloneDXError(f"{ctx}: missing required field {key!r}")
    val = obj[key]
    if not isinstance(val, expected_type):
        name = expected_type.__name__ if isinstance(expected_type, type) else str(expected_type)
        raise CycloneDXError(f"{ctx}: field {key!r} must be {name}")
    return val


def parse_cyclonedx(doc: dict) -> CycloneDX:
    if not isinstance(doc, dict):
        raise CycloneDXError("SBOM document must be a JSON object")
    fmt = _require(doc, "bomFormat", "document", str)
    if fmt != "CycloneDX":
        raise CycloneDXError(f"unsupported bomFormat {fmt!r}; only CycloneDX is accepted")
    spec = _require(doc, "specVersion", "document", str)
    if not spec.startswith(("1.",)):
        raise CycloneDXError(f"unsupported specVersion {spec!r}; supported: 1.4 / 1.5 / 1.6")
    if spec not in ("1.4", "1.5", "1.6"):
        raise CycloneDXError(f"unsupported specVersion {spec!r}; supported: 1.4 / 1.5 / 1.6")

    warnings: list[str] = []
    components = doc.get("components", [])
    if not isinstance(components, list):
        raise CycloneDXError("'components' must be an array")
    dependencies = doc.get("dependencies", [])
    if not isinstance(dependencies, list):
        raise CycloneDXError("'dependencies' must be an array")

    nodes: dict[tuple, Node] = {}
    ref_to_key: dict[str, tuple] = {}

    for i, comp in enumerate(components):
        ctx = f"components[{i}]"
        if not isinstance(comp, dict):
            raise CycloneDXError(f"{ctx} must be an object")
        name = _require(comp, "name", ctx, str)
        ctype = _require(comp, "type", ctx, str)
        if ctype not in _VALID_TYPES:
            raise CycloneDXError(f"{ctx}: unknown component type {ctype!r}")
        scope = comp.get("scope", "required")
        if scope not in _VALID_SCOPES:
            raise CycloneDXError(
                f"{ctx}: scope {scope!r} must be one of {sorted(_VALID_SCOPES)}")

        raw_purl = comp.get("purl")
        if raw_purl is not None and not isinstance(raw_purl, str):
            raise CycloneDXError(f"{ctx}: 'purl' must be a string")
        purl, purl_error = None, None
        if raw_purl:
            try:
                purl = parse_purl(raw_purl)
            except PurlError as exc:
                purl_error = str(exc)
                warnings.append(f"{ctx} ({name}): invalid purl ignored for matching: {exc}")

        version = comp.get("version")
        if version is not None and not isinstance(version, str):
            raise CycloneDXError(f"{ctx}: 'version' must be a string")
        if purl is not None and purl.version is not None:
            version = purl.version

        if purl is not None:
            key = ("purl", purl.type, purl.namespace, purl.name,
                   purl.version, tuple(sorted(purl.qualifiers.items())),
                   purl.subpath, scope)
        else:
            # components without a usable purl get a synthetic identity
            key = ("anon", ctype, name.lower(), version, scope,
                   raw_purl if raw_purl else None)

        ref = comp.get("bom-ref")
        if ref is not None and not isinstance(ref, str):
            raise CycloneDXError(f"{ctx}: 'bom-ref' must be a string")

        if key in nodes:
            node = nodes[key]
            node.merged_count += 1
            if raw_purl and raw_purl not in node.raw_purls:
                node.raw_purls.append(raw_purl)
        else:
            node = Node(key=key, component_type=ctype, cdx_name=name, scope=scope,
                        purl=purl, purl_error=purl_error, version=version,
                        raw_purls=[raw_purl] if raw_purl else [])
            nodes[key] = node
        if ref:
            node.bom_refs.add(ref)
            if ref in ref_to_key and ref_to_key[ref] != key:
                raise CycloneDXError(f"bom-ref {ref!r} maps to multiple components")
            ref_to_key[ref] = key

    # ---------------------------------------------------------------- graph
    edges: dict[str, set[str]] = {}
    known_refs = set(ref_to_key)
    root_ref = None
    meta = doc.get("metadata")
    if isinstance(meta, dict) and isinstance(meta.get("component"), dict):
        root_ref = meta["component"].get("bom-ref")
        if root_ref is not None and not isinstance(root_ref, str):
            raise CycloneDXError("metadata.component.bom-ref must be a string")
        if root_ref:
            known_refs.add(root_ref)  # the described component may have no entry

    for i, dep in enumerate(dependencies):
        ctx = f"dependencies[{i}]"
        if not isinstance(dep, dict):
            raise CycloneDXError(f"{ctx} must be an object")
        ref = _require(dep, "ref", ctx, str)
        depends = dep.get("dependsOn", [])
        if not isinstance(depends, list) or not all(isinstance(x, str) for x in depends):
            raise CycloneDXError(f"{ctx}: 'dependsOn' must be an array of strings")
        if ref not in known_refs:
            warnings.append(f"{ctx}: dependency ref {ref!r} is not declared as a component")
        edges.setdefault(ref, set())
        for target in depends:
            if target not in known_refs:
                warnings.append(
                    f"{ctx}: dependsOn target {target!r} is not declared as a component")
            edges[ref].add(target)

    document_ref = doc.get("serialNumber") or doc.get("bom-ref")
    if document_ref is not None and not isinstance(document_ref, str):
        raise CycloneDXError("document identifier must be a string")

    return CycloneDX(spec_version=spec, document_ref=document_ref, root_ref=root_ref,
                     nodes=nodes, ref_to_key=ref_to_key, edges=edges,
                     metadata=meta if isinstance(meta, dict) else {},
                     warnings=warnings)
