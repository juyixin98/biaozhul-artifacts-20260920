"""Core matching engine: normalized components + graph + local advisories.

Statuses produced per component/advisory pair
---------------------------------------------
``affected``        version satisfies an affected range (real version compare)
``not_affected``    version compared successfully but matched no range
``missing_version`` component/purl carries no usable version -> unknown
``invalid_version`` version string cannot be parsed for its ecosystem
``invalid_purl``    component purl is malformed
``unsupported_ecosystem``
``no_fixture_data`` package absent from the local fixture (NOT "clean";
                    the fixture is not a complete feed)
``excluded``        component scope is ``excluded``; not evaluated

Components are evaluated against advisories only when the fixture knows
the package. A package with zero advisories is reported once as
``no_fixture_data`` so the absence of data is never confused with safety.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .cyclonedx import CycloneDX, Node
from .fixtures import Advisory
from .graph import Graph
from .versions import satisfies
from .versions.errors import VersionError


@dataclass
class ComponentReport:
    node_key: str
    bom_refs: list[str]
    purl: str | None
    ecosystem: str | None
    name: str | None
    namespace: list[str]
    version: str | None
    scope: str
    relation: str           # direct | transitive | unreferenced
    merged_count: int
    variant: dict


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _relation_for(key, graph: Graph) -> str:
    if key in graph.direct_keys:
        return "direct"
    if key in graph.reachable_keys:
        return "transitive"
    return "unreferenced"


def _evidence_for(key, graph: Graph) -> list[list[str]]:
    return graph.shortest_paths.get(key, [])


def analyze(cdx: CycloneDX, graph: Graph, advisories: list[Advisory]):
    index: dict[tuple, list[Advisory]] = {}
    for adv in advisories:
        index.setdefault(adv.package_key(), []).append(adv)

    component_reports: list[ComponentReport] = []
    findings: list[dict] = []

    for key, node in cdx.nodes.items():
        relation = _relation_for(key, graph)
        report = ComponentReport(
            node_key=repr(key),
            bom_refs=sorted(node.bom_refs),
            purl=node.display_purl(),
            ecosystem=node.purl.type if node.purl else None,
            name=node.purl.name if node.purl else node.cdx_name,
            namespace=list(node.purl.namespace) if node.purl else [],
            version=node.version,
            scope=node.scope,
            relation=relation,
            merged_count=node.merged_count,
            variant={"qualifiers": dict(node.purl.qualifiers) if node.purl else {},
                     "subpath": list(node.purl.subpath) if node.purl else []},
        )
        component_reports.append(report)

        base = {"node_key": report.node_key, "component": report.name,
                "ecosystem": report.ecosystem, "version": report.version,
                "relation": relation,
                "evidence_paths": _evidence_for(key, graph)}

        # excluded components are retained but never evaluated
        if node.scope == "excluded":
            findings.append({**base, "vulnerability_id": None, "range_id": None,
                             "status": "excluded",
                             "detail": "component scope is 'excluded' in the SBOM"})
            continue

        if node.purl is None:
            findings.append({**base, "vulnerability_id": None, "range_id": None,
                             "status": "invalid_purl",
                             "detail": node.purl_error or "component has no purl; cannot match"})
            continue

        purl = node.purl
        if purl.type not in ("npm", "pypi", "maven"):
            findings.append({**base, "vulnerability_id": None, "range_id": None,
                             "status": "unsupported_ecosystem",
                             "detail": f"no version comparison rules for ecosystem {purl.type!r}"})
            continue

        if not node.version:
            findings.append({**base, "vulnerability_id": None, "range_id": None,
                             "status": "missing_version",
                             "detail": "component declares no version; affectedness is unknown"})
            continue

        advs = index.get((purl.type, purl.namespace, purl.name), [])
        if not advs and purl.type == "maven":
            # Maven groupIds commonly appear as a single dotted segment
            flat = tuple(part for seg in purl.namespace for part in seg.split("."))
            advs = index.get((purl.type, flat, purl.name), [])
        if not advs:
            findings.append({**base, "vulnerability_id": None, "range_id": None,
                             "status": "no_fixture_data",
                             "detail": "local fixture contains no advisories for this package;"
                                       " this is not evidence that it is unaffected"})
            continue

        for adv in advs:
            matched_any = False
            parse_failed = False
            for rng in adv.ranges:
                if rng.ecosystem != purl.type:
                    continue
                try:
                    hit = satisfies(purl.type, node.version, rng.expression)
                except VersionError as exc:
                    parse_failed = True
                    findings.append({**base, "vulnerability_id": adv.vulnerability_id,
                                     "range_id": rng.range_id, "status": "invalid_version",
                                     "detail": f"version/range could not be compared: {exc}"})
                    continue
                if hit:
                    matched_any = True
                    findings.append({**base, "vulnerability_id": adv.vulnerability_id,
                                     "range_id": rng.range_id, "status": "affected",
                                     "severity": adv.severity,
                                     "summary": adv.summary,
                                     "range_expression": rng.expression,
                                     "detail": rng.source_note})
            if not matched_any and not parse_failed:
                findings.append({**base, "vulnerability_id": adv.vulnerability_id,
                                 "range_id": None, "status": "not_affected",
                                 "detail": "version is outside every affected range in the local fixture"})

    return component_reports, findings
