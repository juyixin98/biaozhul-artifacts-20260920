#include "scc.hpp"

#include <algorithm>
#include <queue>
#include <unordered_map>
#include <utility>
#include <vector>

namespace scc {

namespace {

// First Kosaraju pass: iterative DFS postorder on G.
// Explicit stack keeps this safe for n = 100k (no recursion depth risk).
std::vector<int> finishOrder(const Graph& g) {
    const int n = g.n;
    std::vector<char> visited(static_cast<std::size_t>(n), 0);
    std::vector<int> order;
    order.reserve(n);
    // Stack frame: (vertex, next adjacency index).
    std::vector<std::pair<int, int>> stack;

    for (int start = 0; start < n; ++start) {
        if (visited[static_cast<std::size_t>(start)]) continue;
        visited[static_cast<std::size_t>(start)] = 1;
        stack.emplace_back(start, 0);
        while (!stack.empty()) {
            int u = stack.back().first;
            int& idx = stack.back().second;
            while (idx < static_cast<int>(g.adj[u].size()) &&
                   visited[static_cast<std::size_t>(g.adj[u][static_cast<std::size_t>(idx)].first)]) {
                ++idx;
            }
            if (idx == static_cast<int>(g.adj[u].size())) {
                order.push_back(u);
                stack.pop_back();
            } else {
                int v = g.adj[u][static_cast<std::size_t>(idx)].first;
                ++idx;
                visited[static_cast<std::size_t>(v)] = 1;
                stack.emplace_back(v, 0);
            }
        }
    }
    return order;
}

// Shortest cycle through `root`, restricted to vertices of component `cid`.
// BFS for the shortest path root -> u whose edge u -> root closes the cycle.
// Returns vertices [root, ..., u] (the edge u -> root is implicit); for a
// self-loop returns {root}. Deterministic: adjacency lists are vertex-sorted.
std::vector<int> cycleWitness(const Graph& g, int root, int cid,
                              const std::vector<int>& compOf) {
    // Self-loop is the shortest possible witness.
    for (const auto& [v, mult] : g.adj[root]) {
        (void)mult;
        if (v == root) return {root};
    }

    const int n = g.n;
    std::vector<int> parent(static_cast<std::size_t>(n), -1);
    std::vector<char> seen(static_cast<std::size_t>(n), 0);
    std::queue<int> q;
    seen[static_cast<std::size_t>(root)] = 1;
    q.push(root);

    int tail = -1;
    while (!q.empty() && tail < 0) {
        int u = q.front();
        q.pop();
        for (const auto& [v, mult] : g.adj[u]) {
            (void)mult;
            if (compOf[static_cast<std::size_t>(v)] != cid) continue;
            if (v == root && u != root) { tail = u; break; }
            if (!seen[static_cast<std::size_t>(v)]) {
                seen[static_cast<std::size_t>(v)] = 1;
                parent[static_cast<std::size_t>(v)] = u;
                q.push(v);
            }
        }
    }

    // An SCC of size >= 2 (or singleton self-loop, handled above) always
    // contains a cycle through root, so BFS must find one.
    std::vector<int> cycle;
    for (int x = tail; x != -1 && x != root; x = parent[static_cast<std::size_t>(x)])
        cycle.push_back(x);
    std::reverse(cycle.begin(), cycle.end());
    cycle.insert(cycle.begin(), root);
    return cycle;
}

} // namespace

AnalysisResult analyze(const Graph& g) {
    const int n = g.n;

    // ---- Kosaraju pass 1: finish order on G ----
    std::vector<int> order = finishOrder(g);

    // ---- Kosaraju pass 2: DFS on G^T in reverse finish order ----
    std::vector<int> rawComp(static_cast<std::size_t>(n), -1);
    std::vector<std::vector<int>> rawComponents;
    std::vector<int> stack;
    for (auto it = order.rbegin(); it != order.rend(); ++it) {
        int start = *it;
        if (rawComp[static_cast<std::size_t>(start)] != -1) continue;
        int cid = static_cast<int>(rawComponents.size());
        rawComponents.emplace_back();
        rawComp[static_cast<std::size_t>(start)] = cid;
        stack.push_back(start);
        while (!stack.empty()) {
            int u = stack.back();
            stack.pop_back();
            rawComponents[static_cast<std::size_t>(cid)].push_back(u);
            for (const auto& [v, mult] : g.radj[u]) {
                (void)mult;
                if (rawComp[static_cast<std::size_t>(v)] == -1) {
                    rawComp[static_cast<std::size_t>(v)] = cid;
                    stack.push_back(v);
                }
            }
        }
    }

    // ---- Canonical component numbering ----
    // Final ids are assigned by ascending minimum vertex, so results stay
    // identical regardless of the order edges happened to be listed in.
    const int componentCount = static_cast<int>(rawComponents.size());
    std::vector<int> remap(static_cast<std::size_t>(componentCount), -1);
    std::vector<int> mins(static_cast<std::size_t>(componentCount), n);
    for (int c = 0; c < componentCount; ++c)
        for (int v : rawComponents[static_cast<std::size_t>(c)])
            mins[static_cast<std::size_t>(c)] = std::min(mins[static_cast<std::size_t>(c)], v);
    std::vector<int> rawOrder(static_cast<std::size_t>(componentCount));
    for (int i = 0; i < componentCount; ++i) rawOrder[static_cast<std::size_t>(i)] = i;
    std::sort(rawOrder.begin(), rawOrder.end(),
              [&](int a, int b) { return mins[static_cast<std::size_t>(a)] <
                                              mins[static_cast<std::size_t>(b)]; });
    for (int finalId = 0; finalId < componentCount; ++finalId)
        remap[static_cast<std::size_t>(rawOrder[static_cast<std::size_t>(finalId)])] = finalId;

    AnalysisResult result;
    result.compOf.assign(static_cast<std::size_t>(n), -1);
    for (int v = 0; v < n; ++v)
        result.compOf[static_cast<std::size_t>(v)] =
            remap[static_cast<std::size_t>(rawComp[static_cast<std::size_t>(v)])];

    result.components.resize(static_cast<std::size_t>(componentCount));
    for (int c = 0; c < componentCount; ++c) {
        int finalId = remap[static_cast<std::size_t>(c)];
        auto& verts = rawComponents[static_cast<std::size_t>(c)];
        std::sort(verts.begin(), verts.end());
        result.components[static_cast<std::size_t>(finalId)] = {
            finalId, verts, verts.front()};
    }

    // ---- Condensation edges: aggregate cross-SCC edges, dedup by (ci,cj) ----
    std::unordered_map<std::uint64_t, std::int64_t> aggregated;
    aggregated.reserve(g.uniqueEdges.size() * 2);
    for (const Edge& e : g.uniqueEdges) {
        int cu = result.compOf[static_cast<std::size_t>(e.from)];
        int cv = result.compOf[static_cast<std::size_t>(e.to)];
        if (cu == cv) continue;
        auto key = static_cast<std::uint64_t>(static_cast<std::uint32_t>(cu)) << 32 |
                   static_cast<std::uint32_t>(cv);
        aggregated[key] += e.multiplicity;
    }
    result.dagEdges.reserve(aggregated.size());
    for (const auto& [key, mult] : aggregated) {
        int cu = static_cast<int>(key >> 32);
        int cv = static_cast<int>(static_cast<std::uint32_t>(key));
        result.dagEdges.push_back({cu, cv, mult});
    }
    std::sort(result.dagEdges.begin(), result.dagEdges.end(),
              [](const DagEdge& a, const DagEdge& b) {
                  return a.fromComponent != b.fromComponent
                             ? a.fromComponent < b.fromComponent
                             : a.toComponent < b.toComponent;
              });

    // ---- Cycle witnesses ----
    result.witnesses.resize(static_cast<std::size_t>(componentCount));
    for (const Component& comp : result.components) {
        bool cyclic = comp.vertices.size() >= 2;
        if (!cyclic) {
            int v = comp.representative;
            for (const auto& [to, mult] : g.adj[v]) {
                (void)mult;
                if (to == v) { cyclic = true; break; }
            }
        }
        if (cyclic)
            result.witnesses[static_cast<std::size_t>(comp.id)] =
                cycleWitness(g, comp.representative, comp.id, result.compOf);
    }
    return result;
}

} // namespace scc
