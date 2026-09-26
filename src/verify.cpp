#include "verify.hpp"

#include <algorithm>
#include <queue>
#include <unordered_map>
#include <unordered_set>

namespace verify {

namespace {

// Plain BFS reachability in g, optionally restricted to vertices of one
// component. Independent from both the solver and from naive.cpp.
bool bfsReaches(const Graph& g, int src, int dst, const std::vector<int>* restrictTo = nullptr) {
    std::vector<char> seen(static_cast<std::size_t>(g.n), 0);
    std::queue<int> q;
    seen[static_cast<std::size_t>(src)] = 1;
    q.push(src);
    while (!q.empty()) {
        int u = q.front();
        q.pop();
        if (u == dst) return true;
        for (const auto& [v, mult] : g.adj[u]) {
            (void)mult;
            if (restrictTo && (*restrictTo)[static_cast<std::size_t>(v)] !=
                                  (*restrictTo)[static_cast<std::size_t>(src)])
                continue;
            if (!seen[static_cast<std::size_t>(v)]) {
                seen[static_cast<std::size_t>(v)] = 1;
                q.push(v);
            }
        }
    }
    return false;
}

} // namespace

CheckReport checkResult(const Graph& g, const AnalysisResult& r) {
    CheckReport rep;
    const int n = g.n;

    // ---- 1. Partition validity: every vertex appears exactly once ----
    std::vector<int> seen(static_cast<std::size_t>(n), 0);
    if (static_cast<int>(r.components.size()) > n)
        rep.fail("more components than vertices");
    for (const Component& c : r.components) {
        if (c.id < 0 || c.id >= static_cast<int>(r.components.size()))
            rep.fail("component id out of range: " + std::to_string(c.id));
        if (c.vertices.empty()) rep.fail("component " + std::to_string(c.id) + " is empty");
        if (!std::is_sorted(c.vertices.begin(), c.vertices.end()))
            rep.fail("component " + std::to_string(c.id) + " vertices not sorted");
        if (std::adjacent_find(c.vertices.begin(), c.vertices.end()) != c.vertices.end())
            rep.fail("component " + std::to_string(c.id) + " has duplicate vertices");
        if (c.representative != c.vertices.front())
            rep.fail("component " + std::to_string(c.id) + " representative mismatch");
        for (int v : c.vertices) {
            if (v < 0 || v >= n) rep.fail("vertex id out of range: " + std::to_string(v));
            if (seen[static_cast<std::size_t>(v)]) rep.fail("vertex " + std::to_string(v) + " in two components");
            seen[static_cast<std::size_t>(v)] = 1;
            if (r.compOf[static_cast<std::size_t>(v)] != c.id)
                rep.fail("compOf[" + std::to_string(v) + "] inconsistent with component list");
        }
    }
    for (int v = 0; v < n; ++v)
        if (!seen[static_cast<std::size_t>(v)]) rep.fail("vertex " + std::to_string(v) + " belongs to no component");

    // Canonical numbering: ids ascending by smallest vertex.
    for (int i = 1; i < static_cast<int>(r.components.size()); ++i)
        if (r.components[static_cast<std::size_t>(i - 1)].representative >
            r.components[static_cast<std::size_t>(i)].representative)
            rep.fail("component ids not assigned by ascending minimum vertex");

    // ---- 2. Strong connectivity of each component ----
    for (const Component& c : r.components) {
        for (int v : c.vertices) {
            if (!bfsReaches(g, c.representative, v, &r.compOf) ||
                !bfsReaches(g, v, c.representative, &r.compOf)) {
                rep.fail("component " + std::to_string(c.id) + " is not strongly connected at vertex " +
                         std::to_string(v));
                break;
            }
        }
    }

    // ---- 3. Maximality (small graphs only): independent BFS-based partition ----
    if (n <= limits::MAX_NAIVE_N) {
        // Label each vertex by the set of vertices that reach it and it reaches.
        // Two vertices share an SCC iff their reach sets contain each other.
        std::vector<std::vector<char>> reach(static_cast<std::size_t>(n));
        for (int s = 0; s < n; ++s) {
            reach[static_cast<std::size_t>(s)].assign(static_cast<std::size_t>(n), 0);
            std::queue<int> q;
            reach[static_cast<std::size_t>(s)][static_cast<std::size_t>(s)] = 1;
            q.push(s);
            while (!q.empty()) {
                int u = q.front();
                q.pop();
                for (const auto& [v, mult] : g.adj[u]) {
                    (void)mult;
                    if (!reach[static_cast<std::size_t>(s)][static_cast<std::size_t>(v)]) {
                        reach[static_cast<std::size_t>(s)][static_cast<std::size_t>(v)] = 1;
                        q.push(v);
                    }
                }
            }
        }
        for (int u = 0; u < n; ++u)
            for (int v = u + 1; v < n; ++v) {
                bool mutual = reach[static_cast<std::size_t>(u)][static_cast<std::size_t>(v)] &&
                              reach[static_cast<std::size_t>(v)][static_cast<std::size_t>(u)];
                bool same = r.compOf[static_cast<std::size_t>(u)] == r.compOf[static_cast<std::size_t>(v)];
                if (mutual && !same)
                    rep.fail("maximality violated: " + std::to_string(u) + " and " + std::to_string(v) +
                             " mutually reachable but split");
                if (same && !mutual)
                    rep.fail("component not strongly connected: " + std::to_string(u) + "," + std::to_string(v));
            }
    }

    // ---- 4. Condensation edges: exact dedup and multiplicity ----
    std::unordered_map<std::uint64_t, std::int64_t> expected;
    for (const Edge& e : g.uniqueEdges) {
        int cu = r.compOf[static_cast<std::size_t>(e.from)];
        int cv = r.compOf[static_cast<std::size_t>(e.to)];
        if (cu == cv) continue;
        auto key = static_cast<std::uint64_t>(static_cast<std::uint32_t>(cu)) << 32 |
                   static_cast<std::uint32_t>(cv);
        expected[key] += e.multiplicity;
    }
    std::unordered_set<std::uint64_t> observed;
    std::int64_t observedRawTotal = 0;
    for (const DagEdge& d : r.dagEdges) {
        if (d.fromComponent == d.toComponent)
            rep.fail("DAG edge is a self-loop on component " + std::to_string(d.fromComponent));
        auto key = static_cast<std::uint64_t>(static_cast<std::uint32_t>(d.fromComponent)) << 32 |
                   static_cast<std::uint32_t>(d.toComponent);
        if (!observed.insert(key).second)
            rep.fail("duplicate condensation edge " + std::to_string(d.fromComponent) + "->" +
                     std::to_string(d.toComponent));
        auto it = expected.find(key);
        if (it == expected.end())
            rep.fail("condensation edge with no supporting graph edge: " +
                     std::to_string(d.fromComponent) + "->" + std::to_string(d.toComponent));
        else if (it->second != d.multiplicity)
            rep.fail("multiplicity mismatch on " + std::to_string(d.fromComponent) + "->" +
                     std::to_string(d.toComponent) + ": got " + std::to_string(d.multiplicity) +
                     ", expected " + std::to_string(it->second));
        observedRawTotal += d.multiplicity;
    }
    if (observed.size() != expected.size())
        rep.fail("missing condensation edges: got " + std::to_string(observed.size()) + ", expected " +
                 std::to_string(expected.size()));
    std::int64_t expectedCrossRaw = 0;
    for (const auto& [k, mult] : expected) (void)k, expectedCrossRaw += mult;
    if (observedRawTotal != expectedCrossRaw)
        rep.fail("cross-component raw edge total mismatch");

    if (!std::is_sorted(r.dagEdges.begin(), r.dagEdges.end(),
                        [](const DagEdge& a, const DagEdge& b) {
                            return a.fromComponent != b.fromComponent
                                       ? a.fromComponent < b.fromComponent
                                       : a.toComponent < b.toComponent;
                        }))
        rep.fail("DAG edges not deterministically sorted");

    // ---- 5. Acyclicity of the condensation (independent Kahn topo sort) ----
    const int componentCount = static_cast<int>(r.components.size());
    std::vector<int> indegree(static_cast<std::size_t>(componentCount), 0);
    std::vector<std::vector<int>> dagAdj(static_cast<std::size_t>(componentCount));
    for (const DagEdge& d : r.dagEdges) {
        ++indegree[static_cast<std::size_t>(d.toComponent)];
        dagAdj[static_cast<std::size_t>(d.fromComponent)].push_back(d.toComponent);
    }
    std::queue<int> q;
    for (int c = 0; c < componentCount; ++c)
        if (indegree[static_cast<std::size_t>(c)] == 0) q.push(c);
    int processed = 0;
    while (!q.empty()) {
        int c = q.front();
        q.pop();
        ++processed;
        for (int nxt : dagAdj[static_cast<std::size_t>(c)])
            if (--indegree[static_cast<std::size_t>(nxt)] == 0) q.push(nxt);
    }
    if (processed != componentCount) rep.fail("condensation contains a directed cycle");

    // ---- 6. Cycle witnesses ----
    auto hasEdge = [&](int u, int v) {
        return std::any_of(g.adj[static_cast<std::size_t>(u)].begin(),
                           g.adj[static_cast<std::size_t>(u)].end(),
                           [v](const auto& e) { return e.first == v; });
    };
    for (const Component& c : r.components) {
        const std::vector<int>& w = r.witnesses[static_cast<std::size_t>(c.id)];
        bool shouldBeCyclic = c.vertices.size() >= 2 || hasEdge(c.representative, c.representative);
        if (!shouldBeCyclic) {
            if (!w.empty()) rep.fail("acyclic component " + std::to_string(c.id) + " has a witness");
            continue;
        }
        if (w.empty()) { rep.fail("cyclic component " + std::to_string(c.id) + " missing witness"); continue; }
        if (w.front() != c.representative)
            rep.fail("witness of component " + std::to_string(c.id) + " must start at representative");
        for (int v : w)
            if (r.compOf[static_cast<std::size_t>(v)] != c.id)
                rep.fail("witness of component " + std::to_string(c.id) + " leaves the component");
        if (std::unordered_set<int>(w.begin(), w.end()).size() != w.size())
            rep.fail("witness of component " + std::to_string(c.id) + " repeats a vertex");
        for (std::size_t i = 0; i < w.size(); ++i) {
            int u = w[i];
            int v = w[(i + 1) % w.size()]; // closing edge back to the first vertex
            if (!hasEdge(u, v))
                rep.fail("witness of component " + std::to_string(c.id) + " uses missing edge " +
                         std::to_string(u) + "->" + std::to_string(v));
        }
    }
    return rep;
}

} // namespace verify
