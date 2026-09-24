"""依赖图：邻接构建、传递依赖环检测、受影响路径枚举。

- 依赖边 pkg -> dependency。
- 根集合：显式 ``is_root=true`` 的包；若没有任何显式标记，则取入度为 0 的包。
- 环：Tarjan 强连通分量，size>1 的分量以及自环构成的环。
- 路径：从每个根 DFS 到目标节点的所有简单路径（环上节点不重复进入），
  设上限防止病态图爆炸。路径中的包以身份键表示。
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Set, Tuple

from .models import SBOM, SBOMPackage, make_package_id

# 单目标最多返回路径条数，单条路径最大长度（节点数）
MAX_PATHS_PER_TARGET = 50
MAX_PATH_DEPTH = 40


class GraphError(ValueError):
    """SBOM 依赖图结构错误（重复节点、悬空依赖等）。"""


@dataclass
class DependencyGraph:
    packages: Dict[str, SBOMPackage]
    adjacency: Dict[str, List[str]]
    roots: List[str]
    cycles: List[List[str]] = field(default_factory=list)

    @property
    def node_ids(self) -> Set[str]:
        return set(self.packages)

    def find_paths_to(self, target: str) -> List[List[str]]:
        """枚举 roots -> target 的全部简单路径（受上限约束）。

        target 自身是根时返回 ``[[target]]``；不可达时返回 ``[]``。
        """
        if target not in self.packages:
            return []
        results: List[List[str]] = []

        def dfs(node: str, path: List[str], on_stack: Set[str]) -> None:
            if len(results) >= MAX_PATHS_PER_TARGET or len(path) >= MAX_PATH_DEPTH:
                return
            if node == target:
                results.append(list(path))
                return
            for nxt in self.adjacency.get(node, []):
                if nxt not in self.packages or nxt in on_stack:
                    continue
                on_stack.add(nxt)
                path.append(nxt)
                dfs(nxt, path, on_stack)
                path.pop()
                on_stack.remove(nxt)
                if len(results) >= MAX_PATHS_PER_TARGET:
                    return

        for root in self.roots:
            if len(results) >= MAX_PATHS_PER_TARGET:
                break
            dfs(root, [root], {root})
        return results

    def transitive_dependencies(self, node: str) -> Set[str]:
        """从 node 出发可达的全部节点（含自身），环安全。"""
        seen: Set[str] = set()
        stack = [node]
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            stack.extend(n for n in self.adjacency.get(cur, []) if n in self.packages)
        return seen


def build_graph(sbom: SBOM) -> DependencyGraph:
    packages: Dict[str, SBOMPackage] = {}
    for pkg in sbom.packages:
        if pkg.id in packages:
            raise GraphError(f"重复的包节点（同生态/同名/同版本出现两次）: {pkg.id}")
        packages[pkg.id] = pkg

    adjacency: Dict[str, List[str]] = {pid: [] for pid in packages}
    indegree: Dict[str, int] = defaultdict(int)
    explicit_roots: List[str] = []

    for pid, pkg in packages.items():
        if pkg.is_root is True:
            explicit_roots.append(pid)
        for dep_ref in pkg.dependencies:
            if dep_ref not in packages:
                raise GraphError(f"包 {pid} 依赖了不存在的包标识: {dep_ref!r}")
            adjacency[pid].append(dep_ref)
            indegree[dep_ref] += 1

    if explicit_roots:
        roots = explicit_roots
    else:
        roots = sorted(pid for pid in packages if indegree[pid] == 0)

    graph = DependencyGraph(packages=packages, adjacency=adjacency, roots=roots)
    graph.cycles = _find_cycles(graph)
    return graph


def _find_cycles(graph: DependencyGraph) -> List[List[str]]:
    """Tarjan SCC，返回所有 size>1 的分量与自环。每个环内节点按字母序排列以便断言。"""
    index_counter = [0]
    stack: List[str] = []
    on_stack: Set[str] = set()
    indices: Dict[str, int] = {}
    lowlink: Dict[str, int] = {}
    cycles: List[List[str]] = []

    def strongconnect(v: str) -> None:
        indices[v] = index_counter[0]
        lowlink[v] = index_counter[0]
        index_counter[0] += 1
        stack.append(v)
        on_stack.add(v)

        for w in graph.adjacency[v]:
            if w not in indices:
                strongconnect(w)
                lowlink[v] = min(lowlink[v], lowlink[w])
            elif w in on_stack:
                lowlink[v] = min(lowlink[v], indices[w])

        if lowlink[v] == indices[v]:
            component: List[str] = []
            while True:
                w = stack.pop()
                on_stack.remove(w)
                component.append(w)
                if w == v:
                    break
            if len(component) > 1:
                cycles.append(sorted(component))
            elif v in graph.adjacency[v]:  # 自环
                cycles.append([v])

    for node in sorted(graph.packages):
        if node not in indices:
            strongconnect(node)
    cycles.sort()
    return cycles
