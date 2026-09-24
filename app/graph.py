"""Dependency-graph analysis over normalized CycloneDX components.

* direct dependency   — immediately referenced by ``metadata.component``
                        (or, without a root, any component with no incoming
                        edge)
* transitive dependency — reachable from a direct dependency
* evidence paths      — concrete bom-ref chains root -> ... -> component,
                        computed by BFS (terminates on cycles)
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from .cyclonedx import CycloneDX


@dataclass
class Graph:
    node_edges: dict[tuple, set[tuple]]     # node key -> node keys
    refs_of_key: dict[tuple, list[str]]
    representative_ref: dict[tuple, str]
    direct_keys: set[tuple]
    reachable_keys: set[tuple]
    cycles: list[list[str]]
    shortest_paths: dict[tuple, list[list[str]]]


def build_graph(cdx: CycloneDX) -> Graph:
    refs_of_key: dict[tuple, list[str]] = {}
    representative_ref: dict[tuple, str] = {}
    for ref, key in cdx.ref_to_key.items():
        refs_of_key.setdefault(key, []).append(ref)
        representative_ref.setdefault(key, ref)

    node_edges: dict[tuple, set[tuple]] = {k: set() for k in cdx.nodes}
    for src_ref, targets in cdx.edges.items():
        src_key = cdx.ref_to_key.get(src_ref)
        if src_key is None:
            # edges out of metadata.component (root) have no node key
            continue
        for t in targets:
            tgt_key = cdx.ref_to_key.get(t)
            if tgt_key is not None and tgt_key != src_key:
                node_edges[src_key].add(tgt_key)

    # -------- direct dependency determination
    direct_keys: set[tuple] = set()
    root_ref = cdx.root_ref
    if root_ref is not None:
        for t in cdx.edges.get(root_ref, ()):
            k = cdx.ref_to_key.get(t)
            if k is not None:
                direct_keys.add(k)
    else:
        incoming: set[tuple] = set()
        for targets in node_edges.values():
            incoming.update(targets)
        direct_keys = {k for k in cdx.nodes if k not in incoming}

    # -------- BFS shortest paths (bom-ref chains, starting at root/direct)
    start_paths: list[list[str]] = []
    if root_ref is not None:
        start_paths.append([root_ref])
    else:
        for k in sorted(direct_keys, key=repr):
            start_paths.append([representative_ref[k]])

    shortest: dict[tuple, list[list[str]]] = {}
    best_len: dict[tuple, int] = {}
    reachable: set[tuple] = set()
    queue: deque[tuple[str, list[str]]] = deque((s, p) for p in start_paths for s in [p[0]])

    while queue:
        ref, path = queue.popleft()
        key = cdx.ref_to_key.get(ref)
        if key is not None:
            depth = len(path)
            if key not in best_len or depth < best_len[key]:
                best_len[key] = depth
                shortest[key] = [list(path)]
                reachable.add(key)
            elif depth == best_len[key] and len(shortest[key]) < 3:
                if path not in shortest[key]:
                    shortest[key].append(list(path))
                reachable.add(key)
            elif depth > best_len.get(key, depth):
                # no need to expand a path that already loses to a shorter one
                if depth >= best_len.get(key, 10**9):
                    continue
        for t in sorted(cdx.edges.get(ref, set())):
            if t in path:          # back edge: cycle, do not loop
                continue
            queue.append((t, path + [t]))

    # Evidence paths are reported without the root prefix; a direct
    # dependency evidences itself as a one-element chain.
    normalized: dict[tuple, list[list[str]]] = {}
    for key, paths in shortest.items():
        out = []
        for p in paths:
            chain = p[1:] if p and p[0] == root_ref else p
            if chain:
                out.append(chain)
        if key in direct_keys:
            out = out or [[representative_ref[key]]]
        if out:
            normalized[key] = out

    # -------- cycle detection over node-level graph (DFS)
    cycles = _find_cycles(cdx, node_edges, representative_ref)

    return Graph(
        node_edges=node_edges,
        refs_of_key=refs_of_key,
        representative_ref=representative_ref,
        direct_keys=direct_keys,
        reachable_keys=reachable,
        cycles=cycles,
        shortest_paths=normalized,
    )


def _find_cycles(cdx: CycloneDX, node_edges, representative_ref) -> list[list[str]]:
    """Return distinct node-level cycles as bom-ref chains (closed)."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {k: WHITE for k in cdx.nodes}
    stack: list[tuple] = []
    found: list[tuple] = []

    def visit(key):
        color[key] = GRAY
        stack.append(key)
        for nxt in sorted(node_edges.get(key, ()), key=repr):
            if color[nxt] == GRAY:
                idx = stack.index(nxt)
                cyc = tuple(stack[idx:])
                if cyc not in found:
                    found.append(cyc)
            elif color[nxt] == WHITE:
                visit(nxt)
        stack.pop()
        color[key] = BLACK

    for k in sorted(cdx.nodes, key=repr):
        if color[k] == WHITE:
            visit(k)

    cycles = []
    for cyc in found:
        chain = [representative_ref[k] for k in cyc]
        chain.append(chain[0])
        cycles.append(chain)
    return cycles
