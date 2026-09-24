"""风险匹配主逻辑：漏洞馈送 × SBOM 依赖图。

匹配规则：
- 匹配键是 (ecosystem, name)。同名不同生态（如 npm:json 与 pypi:json）
  永远不会互相命中；同名不同版本是图中的不同节点，各自独立判定。
- 包版本无法解析为 SemVer → 对所有相关漏洞给出 ``unknown``（无法判断）。
- 版本可解析：任一 range（OR）满足即 ``affected``，否则 ``not_affected``。
- 漏洞记录没有给出任何 range → ``unknown``（情报不完整，无法判断）。
- ``affected`` 才枚举从根到该包的依赖路径；``unknown`` 也枚举路径，
  因为“哪里用到了这个未知版本”同样重要。
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Tuple

from .feed import LoadedFeed, Vulnerability
from .graph import MAX_PATHS_PER_TARGET, DependencyGraph, build_graph
from .models import SBOM
from .ranges import VersionRange, parse_range
from .semver import Version, VersionParseError, parse_version


def _package_brief(pkg) -> Dict[str, Any]:
    return {
        "id": pkg.id,
        "ecosystem": pkg.ecosystem,
        "name": pkg.name,
        "version": pkg.version,
    }


def _vuln_brief(vuln: Vulnerability) -> Dict[str, Any]:
    return {
        "id": vuln.id,
        "severity": vuln.severity,
        "title": vuln.title,
        "references": vuln.references,
    }


def analyze_sbom(sbom: SBOM, feed: LoadedFeed) -> Dict[str, Any]:
    graph = build_graph(sbom)

    # (ecosystem, name) -> 预解析区间的漏洞列表；不同生态天然隔离
    advisories: Dict[Tuple[str, str], List[Tuple[Vulnerability, List[VersionRange]]]] = defaultdict(list)
    for vuln in feed.payload.vulnerabilities:
        advisories[(vuln.ecosystem, vuln.name)].append(
            (vuln, [parse_range(r) for r in vuln.ranges])
        )

    findings: List[Dict[str, Any]] = []
    affected_packages = set()
    evaluated_packages = set()
    not_affected_packages = 0
    unknown_findings = 0
    affected_findings = 0

    for pid in sorted(graph.packages):
        pkg = graph.packages[pid]
        key = (pkg.ecosystem, pkg.name)
        if key not in advisories:
            continue
        evaluated_packages.add(pid)

        try:
            version: Version | None = parse_version(pkg.version)
            version_error = None
        except VersionParseError as exc:
            version = None
            version_error = str(exc)

        paths = graph.find_paths_to(pid)
        paths_truncated = len(paths) >= MAX_PATHS_PER_TARGET

        for vuln, ranges in advisories[key]:
            base = {
                "package": _package_brief(pkg),
                "vulnerability": _vuln_brief(vuln),
                "paths": paths,
                "reachable_from_roots": bool(paths),
                "paths_truncated": paths_truncated,
            }

            if version is None:
                findings.append(
                    {
                        **base,
                        "status": "unknown",
                        "reason": "invalid_semver",
                        "detail": version_error,
                        "matched_ranges": [],
                    }
                )
                unknown_findings += 1
            elif not ranges:
                findings.append(
                    {
                        **base,
                        "status": "unknown",
                        "reason": "vulnerability_without_range",
                        "detail": f"漏洞 {vuln.id} 未提供受影响版本区间",
                        "matched_ranges": [],
                    }
                )
                unknown_findings += 1
            else:
                matched = [rng.raw for rng in ranges if rng.satisfies(version)]
                if matched:
                    findings.append(
                        {**base, "status": "affected", "matched_ranges": matched}
                    )
                    affected_findings += 1
                    affected_packages.add(pid)
                else:
                    not_affected_packages += 1

    summary = {
        "packages_total": len(graph.packages),
        "packages_matching_advisory": len(evaluated_packages),
        "affected_packages": len(affected_packages),
        "affected_findings": affected_findings,
        "not_affected_packages": not_affected_packages,
        "unknown_findings": unknown_findings,
        "cycles_count": len(graph.cycles),
    }

    return {
        "feed": {
            "feed_version": feed.payload.feed_version,
            "generated_at": feed.payload.generated_at,
            "verified": feed.verified,
            "kid": feed.kid,
            "vulnerability_count": len(feed.payload.vulnerabilities),
        },
        "summary": summary,
        "roots": graph.roots,
        "cycles": [{"nodes": cycle} for cycle in graph.cycles],
        "findings": findings,
    }
