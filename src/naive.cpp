#include "naive.hpp"

namespace naive {

std::vector<char> reachabilityMatrix(const Graph& g) {
    const int n = g.n;
    std::vector<char> reach(static_cast<std::size_t>(n) * static_cast<std::size_t>(n), 0);
    auto at = [&](int u, int v) -> char& {
        return reach[static_cast<std::size_t>(u) * static_cast<std::size_t>(n) +
                     static_cast<std::size_t>(v)];
    };
    for (int u = 0; u < n; ++u) {
        at(u, u) = 1; // length-zero paths
        for (const auto& [v, mult] : g.adj[u]) {
            (void)mult;
            at(u, v) = 1;
        }
    }
    // Floyd-Warshall transitive closure.
    for (int k = 0; k < n; ++k)
        for (int i = 0; i < n; ++i) {
            if (!at(i, k)) continue;
            for (int j = 0; j < n; ++j)
                if (at(k, j)) at(i, j) = 1;
        }
    return reach;
}

std::vector<int> sccByReachability(const Graph& g) {
    const int n = g.n;
    const std::vector<char> reach = reachabilityMatrix(g);
    auto canReach = [&](int u, int v) -> bool {
        return reach[static_cast<std::size_t>(u) * static_cast<std::size_t>(n) +
                     static_cast<std::size_t>(v)] != 0;
    };

    // Union-find over mutually-reachable pairs.
    std::vector<int> parent(static_cast<std::size_t>(n));
    for (int i = 0; i < n; ++i) parent[static_cast<std::size_t>(i)] = i;
    auto find = [&](int x) {
        while (parent[static_cast<std::size_t>(x)] != x) {
            parent[static_cast<std::size_t>(x)] =
                parent[static_cast<std::size_t>(parent[static_cast<std::size_t>(x)])];
            x = parent[static_cast<std::size_t>(x)];
        }
        return x;
    };
    for (int u = 0; u < n; ++u)
        for (int v = u + 1; v < n; ++v)
            if (canReach(u, v) && canReach(v, u)) {
                int ru = find(u), rv = find(v);
                if (ru != rv)
                    parent[static_cast<std::size_t>(std::max(ru, rv))] = std::min(ru, rv);
            }

    // Canonical numbering by ascending minimum vertex (= find root ordering).
    std::vector<int> remap(static_cast<std::size_t>(n), -1);
    std::vector<int> compOf(static_cast<std::size_t>(n), -1);
    int nextId = 0;
    for (int v = 0; v < n; ++v) {
        int root = find(v);
        if (remap[static_cast<std::size_t>(root)] == -1)
            remap[static_cast<std::size_t>(root)] = nextId++;
        compOf[static_cast<std::size_t>(v)] = remap[static_cast<std::size_t>(root)];
    }
    return compOf;
}

} // namespace naive
