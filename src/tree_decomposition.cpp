#include "tree_decomposition.hpp"

#include <algorithm>
#include <cstdint>

namespace tdw {

TreeDecomposition buildDecomposition(const Graph& g,
                                     const std::vector<int>& order) {
    TreeDecomposition td;
    int n = g.n;

    // position[v] = elimination position of vertex v.
    std::vector<int> position(n, -1);
    for (int i = 0; i < n; ++i) position[order[i]] = i;

    // Replay elimination to learn, for each step i, the later neighbors.
    std::vector<uint64_t> adj = g.adj;
    uint64_t alive = n == 64 ? ~0ULL : (1ULL << n) - 1;
    std::vector<int> parent(n, -1);
    td.bags.resize(n);

    for (int i = 0; i < n; ++i) {
        int v = order[i];
        uint64_t nb = adj[v] & alive;

        td.bags[i].push_back(v);
        int minPos = n + 1;
        uint64_t row = nb;
        while (row) {
            int u = __builtin_ctzll(row);
            td.bags[i].push_back(u);
            minPos = std::min(minPos, position[u]);
            row &= row - 1;
        }
        if (minPos <= n) parent[i] = minPos; // earliest later neighbor's bag

        // Add fill edges so later steps see the chorded graph.
        uint64_t a = nb;
        while (a) {
            int u = __builtin_ctzll(a);
            uint64_t b = a & (a - 1);
            while (b) {
                int w = __builtin_ctzll(b);
                adj[u] |= 1ULL << w;
                adj[w] |= 1ULL << u;
                b &= b - 1;
            }
            a &= a - 1;
        }
        alive &= ~(1ULL << v);
    }

    // A vertex with no later neighbor (last eliminated in its component) is
    // a root of the elimination forest. For disconnected inputs there are
    // several roots; join every extra root to the first root so the output is
    // one tree. Such an edge has empty intersection, which is allowed by the
    // running-intersection property (it only constrains bags sharing a
    // vertex).
    int firstRoot = -1;
    for (int i = 0; i < n; ++i) {
        if (parent[i] == -1) {
            if (firstRoot == -1) {
                firstRoot = i;
            } else {
                td.bagTreeEdges.push_back({firstRoot, i});
            }
            continue;
        }
        td.bagTreeEdges.push_back({parent[i], i});
    }

    for (const auto& bag : td.bags) {
        td.width = std::max(td.width, static_cast<int>(bag.size()) - 1);
    }
    return td;
}

} // namespace tdw
