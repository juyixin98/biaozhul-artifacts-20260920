"""核心匹配逻辑：依赖图、路径枚举、版本范围匹配。"""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import SBOM, Vulnerability
from .semver import RangeSyntaxError, is_full_version, parse_range, parse_version

MAX_PATHS_PER_MATCH = 50  # 每个命中最多返回的依赖路径数，防止组合爆炸


@dataclass
class DependencyGraph:
    """组件依赖图。允许环；悬空引用与重复 id 记入 warnings。"""

    index: dict
    children: dict
    roots: list
    cycles: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


def build_graph(sbom: SBOM) -> DependencyGraph:
    warnings: list[str] = []
    index: dict = {}
    for comp in sbom.components:
        if comp.id in index:
            warnings.append(f"组件 id 重复，后者覆盖前者: {comp.id}")
        index[comp.id] = comp

    children: dict = {c.id: [] for c in sbom.components}
    referenced: set = set()
    for comp in sbom.components:
        for dep in comp.dependencies:
            if dep not in index:
                warnings.append(f"组件 {comp.id} 引用了不存在的依赖 id: {dep}")
                continue
            children[comp.id].append(dep)
            referenced.add(dep)

    roots = [c.id for c in sbom.components if c.id not in referenced]
    if not roots:
        # 纯环图：以所有节点为起点枚举路径，保证受影响组件仍有路径可查
        roots = [c.id for c in sbom.components]
        warnings.append("依赖图没有入度为 0 的根节点（整体成环），路径将从任意节点开始枚举")

    graph = DependencyGraph(index=index, children=children, roots=roots, warnings=warnings)
    graph.cycles = _find_cycles(graph)
    return graph


def _find_cycles(graph: DependencyGraph) -> list[list[str]]:
    """找出图中所有基本回路的代表（每个回路按最小 id 旋转归一后去重）。"""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {cid: WHITE for cid in graph.index}
    cycles: list[list[str]] = []
    seen: set = set()

    def visit(node: str, stack: list[str]) -> None:
        color[node] = GRAY
        stack.append(node)
        for nxt in graph.children.get(node, []):
            if color[nxt] == GRAY:
                start = stack.index(nxt)
                cycle = stack[start:]
                # 旋转归一：以最小 id 开头，避免同一回路因入口不同而重复
                i = cycle.index(min(cycle))
                key = tuple(cycle[i:] + cycle[:i])
                if key not in seen:
                    seen.add(key)
                    cycles.append(list(key))
            elif color[nxt] == WHITE:
                visit(nxt, stack)
        stack.pop()
        color[node] = BLACK

    for cid in graph.index:
        if color[cid] == WHITE:
            visit(cid, [])
    return cycles


def find_paths(graph: DependencyGraph, target: str, cap: int = MAX_PATHS_PER_MATCH) -> list[list[str]]:
    """枚举从根到 target 的所有简单路径（不重复经过节点），最多 cap 条。"""
    paths: list[list[str]] = []

    def dfs(node: str, trail: list[str]) -> None:
        if len(paths) >= cap:
            return
        if node == target:
            paths.append(trail[:])
            return
        for nxt in graph.children.get(node, []):
            if nxt in trail:  # 环保护：简单路径不重复经过节点
                continue
            trail.append(nxt)
            dfs(nxt, trail)
            trail.pop()

    for root in graph.roots:
        if len(paths) >= cap:
            break
        dfs(root, [root])
    return paths


def _key(ecosystem: str, name: str) -> tuple:
    # 匹配键 = (生态, 包名)。生态大小写不敏感；同名不同生态是不同键。
    return (ecosystem.strip().lower(), name)


def match_sbom(sbom: SBOM, vulnerabilities: list[Vulnerability]) -> dict:
    """对 SBOM 执行漏洞匹配，返回报告主体（不含签名）。"""
    graph = build_graph(sbom)
    warnings = list(graph.warnings)

    # 预解析漏洞范围；范围语法错误的漏洞条目跳过并告警
    parsed_vulns = []
    for vuln in vulnerabilities:
        try:
            ranges = [parse_range(r) for r in vuln.ranges]
        except RangeSyntaxError as exc:
            warnings.append(f"漏洞 {vuln.id} 的范围语法无效，已跳过: {exc}")
            continue
        parsed_vulns.append((vuln, ranges))

    vulns_by_key: dict = {}
    for vuln, ranges in parsed_vulns:
        vulns_by_key.setdefault(_key(vuln.package.ecosystem, vuln.package.name), []).append(
            (vuln, ranges)
        )

    matches: list[dict] = []
    unknown: list[dict] = []

    for comp in sbom.components:
        candidates = vulns_by_key.get(_key(comp.ecosystem, comp.name))
        if not candidates:
            continue
        version = None
        reason = None
        if comp.version is None:
            reason = "missing_version"
        elif not is_full_version(comp.version):
            reason = "invalid_semver"
        else:
            try:
                version = parse_version(comp.version)
            except ValueError:
                reason = "invalid_semver"

        if reason is not None:
            unknown.append(
                {
                    "component_id": comp.id,
                    "name": comp.name,
                    "ecosystem": comp.ecosystem,
                    "version": comp.version,
                    "reason": reason,
                    "status": "unknown",
                    "possible_vulnerabilities": [v.id for v, _ in candidates],
                }
            )
            continue

        for vuln, ranges in candidates:
            if any(r.matches(version) for r in ranges):
                paths = find_paths(graph, comp.id)
                matches.append(
                    {
                        "vulnerability_id": vuln.id,
                        "summary": vuln.summary,
                        "component_id": comp.id,
                        "name": comp.name,
                        "ecosystem": comp.ecosystem,
                        "version": comp.version,
                        "status": "affected",
                        "matched_ranges": [r.source for r in ranges if r.matches(version)],
                        "fixed_versions": vuln.fixed_versions,
                        "dependency_paths": paths,
                        "dependency_paths_truncated": len(paths) >= MAX_PATHS_PER_MATCH,
                    }
                )

    return {
        "sbom_name": sbom.name,
        "sbom_format": sbom.format,
        "summary": {
            "components": len(sbom.components),
            "vulnerabilities_checked": len(parsed_vulns),
            "affected": len(matches),
            "unknown": len(unknown),
            "cycles_detected": len(graph.cycles),
        },
        "matches": matches,
        "unknown": unknown,
        "cycles": graph.cycles,
        "warnings": warnings,
    }
