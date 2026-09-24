"""把规范化组件与本地漏洞夹具进行核对，并产出证据路径。"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from . import versions
from .sbom import Component, SbomReport

UNKNOWN_REASONS = {
    "missing_version": "component purl has no version; affected status is unknown",
    "unsupported_ecosystem": "no range comparator exists for this ecosystem; "
                             "affected status is unknown",
    "invalid_version": "version or range could not be parsed by the ecosystem "
                       "rules; affected status is unknown",
    "no_applicable_range": "fixture entry carries no range applicable to this "
                           "variant; affected status is unknown",
}


@dataclass(frozen=True)
class Vulnerability:
    id: str
    ecosystem: str
    namespace: str | None
    name: str
    version_range: str
    range_type: str
    summary: str
    fixed_versions: tuple[str, ...]
    qualifiers: dict[str, str] | None = None  # None=不区分；{}=必须无；其它=精确匹配


def load_fixture(path: str | Path) -> tuple[list[Vulnerability], dict]:
    """加载本地夹具。返回 (漏洞列表, meta)。文件是受信任本地数据，
    但结构仍显式校验，错误直接抛出。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("vulnerabilities"), list):
        raise ValueError(f"malformed fixture file: {path}")
    vulns: list[Vulnerability] = []
    for row in data["vulnerabilities"]:
        for required in ("id", "ecosystem", "name", "version_range"):
            if not row.get(required):
                raise ValueError(f"fixture entry missing {required!r}: {row}")
        namespace = row.get("namespace")
        qualifiers = row.get("qualifiers")
        if qualifiers is not None and not isinstance(qualifiers, dict):
            raise ValueError(f"fixture entry {row['id']} has non-object qualifiers")
        vulns.append(Vulnerability(
            id=row["id"], ecosystem=str(row["ecosystem"]).strip().lower(),
            namespace=str(namespace).lower() if namespace else None,
            name=str(row["name"]).lower(),
            version_range=str(row["version_range"]),
            range_type=str(row.get("range_type", "ecosystem_range")),
            summary=str(row.get("summary", "")),
            fixed_versions=tuple(row.get("fixed_versions") or ()),
            qualifiers={str(k).lower(): str(v) for k, v in qualifiers.items()}
            if isinstance(qualifiers, dict) else None,
        ))
    return vulns, data.get("_meta", {})


def _identity_matches(vuln: Vulnerability, comp: Component) -> bool:
    purl = comp.purl
    if purl.ecosystem != vuln.ecosystem:
        return False
    if purl.name.lower() != vuln.name:
        return False
    ns = (purl.namespace or "").lower()
    if (vuln.namespace or "") != ns:
        return False
    if vuln.qualifiers is not None and dict(purl.qualifiers) != vuln.qualifiers:
        return False
    return True


def _forward_bfs_paths(adj: dict[str, set[str]], source: str,
                       target: str, limit: int) -> list[list[str]]:
    """source 到 target 的最短路径（逐层记录前驱），循环安全。

    整个 BFS 层一起 settle，因此所有等长最短路径（含菱形依赖）都能重建；
    ``limit`` 限制返回条数。
    """
    if source == target:
        return [[source]]

    parents: dict[str, set[str]] = {}
    frontier = {source}
    settled = {source}
    found = False
    while frontier and not found:
        next_frontier: set[str] = set()
        layer_parents: dict[str, set[str]] = {}
        for node in frontier:
            for child in adj.get(node, ()):
                if child in settled:
                    continue
                layer_parents.setdefault(child, set()).add(node)
                next_frontier.add(child)
                if child == target:
                    found = True
        for child, ps in layer_parents.items():
            parents.setdefault(child, set()).update(ps)
        # 整层一起 settle，保证同层等长路径不被提前截断
        settled.update(next_frontier)
        frontier = next_frontier

    if not found:
        return []

    paths: list[list[str]] = []

    def build(node: str, chain: list[str]) -> None:
        """沿前驱回退；chain 为当前节点之后（向 target 方向）的路径。"""
        if len(paths) >= limit:
            return
        if node == source:
            paths.append([source, *chain])
            return
        for p in sorted(parents.get(node, ())):
            build(p, [node, *chain])

    build(target, [])
    return paths[:limit]


def evidence_paths(report: SbomReport, target: str,
                   limit: int = 3) -> list[list[str]]:
    """汇总从所有根到 target 的最短证据路径。"""
    sources = list(report.root_keys)
    if not sources:
        adj = report.adjacency()
        incoming = {k: 0 for k in adj}
        for outs in adj.values():
            for w in outs:
                if w in incoming:
                    incoming[w] += 1
        sources = [k for k, deg in incoming.items() if deg == 0] or list(adj)[:1]
    results: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()
    for source in sources:
        for path in _forward_bfs_paths(report.adjacency(), source, target, limit):
            t = tuple(path)
            if t not in seen:
                seen.add(t)
                results.append(path)
    return results[:limit]


def _component_summary(report: SbomReport, comp: Component) -> dict:
    return comp.to_dict()


def analyze(report: SbomReport, vulns: list[Vulnerability]) -> dict:
    """对全部组件执行核对，返回结构化结论。"""
    findings: list[dict] = []
    unaffected: list[dict] = []
    unknowns: list[dict] = []

    for comp in report.components:
        comp_info = _component_summary(report, comp)
        paths = evidence_paths(report, comp.key)
        path_objs = [
            {"length": len(p) - 1, "purls": [
                report._by_key[k].purl.canonical() if k in report._by_key else k
                for k in p]}
            for p in paths
        ]

        applicable = [vu for vu in vulns if _identity_matches(vu, comp)]
        if not applicable:
            # 即使没有命中的夹具条目，无法按生态规则比较的组件也必须是“未知”，
            # 而不是被当作“未受影响”。
            if comp.purl.ecosystem not in versions.SUPPORTED_ECOSYSTEMS:
                unknowns.append({
                    "component": comp_info,
                    "matched": [{
                        "vulnerability_id": None,
                        "status": "unknown",
                        "reason_code": "unsupported_ecosystem",
                        "reason": UNKNOWN_REASONS["unsupported_ecosystem"]
                                  + f" (ecosystem={comp.purl.ecosystem})",
                        "version_range": None,
                        "range_type": None,
                        "ecosystem": comp.purl.ecosystem,
                        "summary": "no range comparator for this ecosystem",
                        "fixed_versions": [],
                    }],
                    "also_clean": [],
                    "evidence_paths": path_objs,
                })
                continue
            unaffected.append({
                "component": comp_info,
                "matched_vulnerabilities": 0,
                "evidence_paths": path_objs,
            })
            continue

        statuses: list[dict] = []
        for vu in applicable:
            affected, reason = versions.evaluate(
                comp.purl.ecosystem, comp.version, vu.version_range,
                vu.range_type)
            statuses.append((vu, affected, reason))

        comp_unknown = [s for s in statuses if s[2] is not None]
        comp_affected = [s for s in statuses if s[1] and s[2] is None]
        comp_clean = [s for s in statuses if not s[1] and s[2] is None]

        def entry_for(vu: Vulnerability, status: str, reason_code: str | None):
            return {
                "vulnerability_id": vu.id,
                "status": status,
                "reason_code": reason_code,
                "reason": UNKNOWN_REASONS.get(reason_code, reason_code)
                          if reason_code else None,
                "version_range": vu.version_range,
                "range_type": vu.range_type,
                "ecosystem": vu.ecosystem,
                "summary": vu.summary,
                "fixed_versions": list(vu.fixed_versions),
            }

        if comp_affected:
            findings.append({
                "component": comp_info,
                "matched": [entry_for(vu, "affected", None)
                            for vu, _, _ in comp_affected],
                "also_unknown": [entry_for(vu, "unknown", r)
                                 for vu, _, r in comp_unknown],
                "evidence_paths": path_objs,
                "is_transitive": not comp.direct,
                "reachable_from_direct": bool(paths),
            })
        elif comp_unknown:
            unknowns.append({
                "component": comp_info,
                "matched": [entry_for(vu, "unknown", r)
                            for vu, _, r in comp_unknown],
                "also_clean": [entry_for(vu, "not_affected", None)
                               for vu, _, _ in comp_clean],
                "evidence_paths": path_objs,
            })
        else:
            unaffected.append({
                "component": comp_info,
                "matched_vulnerabilities": len(statuses),
                "evidence_paths": path_objs,
            })

    return {
        "affected": findings,
        "unknown": unknowns,
        "not_affected": unaffected,
    }
