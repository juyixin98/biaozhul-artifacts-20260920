#include "elimination.hpp"

#include <algorithm>
#include <cstdint>
#include <limits>

namespace tdw {

namespace {

// Number of edges missing among the alive neighbors of v, i.e. the number of
// fill edges that eliminating v would add right now.
int fillCountFor(int v, const std::vector<uint64_t>& adj, uint64_t alive) {
    uint64_t nb = adj[v] & alive;
    int deg = __builtin_popcountll(nb);
    int present = 0;
    uint64_t row = nb;
    while (row) {
        int u = __builtin_ctzll(row);
        present += __builtin_popcountll(adj[u] & nb);
        row &= row - 1;
    }
    // Every neighbor-neighbor edge was counted from both endpoints.
    int presentEdges = present / 2;
    return deg * (deg - 1) / 2 - presentEdges;
}

} // namespace

ElimResult minFillHeuristic(const Graph& g) {
    ElimResult r;
    int n = g.n;
    std::vector<uint64_t> adj = g.adj;
    uint64_t alive = n == 64 ? ~0ULL : (1ULL << n) - 1;

    for (int step = 0; step < n; ++step) {
        int best = -1;
        int bestFill = std::numeric_limits<int>::max();
        int bestDeg = std::numeric_limits<int>::max();
        int considered = 0;

        uint64_t row = alive;
        while (row) {
            int v = __builtin_ctzll(row);
            ++considered;
            int f = fillCountFor(v, adj, alive);
            int d = __builtin_popcountll(adj[v] & alive);
            if (f < bestFill || (f == bestFill && d < bestDeg) ||
                (f == bestFill && d == bestDeg && v < best)) {
                bestFill = f;
                bestDeg = d;
                best = v;
            }
            row &= row - 1;
        }
        r.candidatesConsidered.push_back(considered);

        // Eliminate `best`: connect all pairs of its alive neighbors.
        uint64_t nb = adj[best] & alive;
        uint64_t a = nb;
        while (a) {
            int u = __builtin_ctzll(a);
            uint64_t b = a & (a - 1);
            while (b) {
                int w = __builtin_ctzll(b);
                if (!g.hasEdge(u, w)) {
                    // Report each fill edge once (canonical orientation).
                    r.fillEdges.push_back({std::min(u, w), std::max(u, w)});
                }
                if (((adj[u] >> w) & 1ULL) == 0) {
                    adj[u] |= 1ULL << w;
                    adj[w] |= 1ULL << u;
                }
                b &= b - 1;
            }
            a &= a - 1;
        }
        // Width contribution = number of later neighbors at elimination.
        r.width = std::max(r.width, __builtin_popcountll(nb));
        r.order.push_back(best);
        alive &= ~(1ULL << best);
    }

    // Fill edges were generated grouped by step; canonicalize for stable
    // output and dedup safety.
    std::sort(r.fillEdges.begin(), r.fillEdges.end());
    r.fillEdges.erase(std::unique(r.fillEdges.begin(), r.fillEdges.end()),
                      r.fillEdges.end());
    return r;
}

ReplayResult replayOrder(const Graph& g, const std::vector<int>& order) {
    ReplayResult r;
    int n = g.n;
    if (static_cast<int>(order.size()) != n) {
        throw std::runtime_error("replayOrder: order must contain every vertex");
    }
    std::vector<int> seen(n, 0);
    for (int v : order) {
        if (v < 0 || v >= n || seen[v]) {
            throw std::runtime_error("replayOrder: order is not a permutation");
        }
        seen[v] = 1;
    }

    std::vector<uint64_t> adj = g.adj;
    std::vector<char> alive(n, 1);
    for (int v : order) {
        std::vector<int> nb;
        for (int u = 0; u < n; ++u) {
            if (alive[u] && ((adj[v] >> u) & 1ULL)) nb.push_back(u);
        }
        r.width = std::max(r.width, static_cast<int>(nb.size()));
        for (size_t i = 0; i < nb.size(); ++i) {
            for (size_t j = i + 1; j < nb.size(); ++j) {
                int u = nb[i], w = nb[j];
                if (!g.hasEdge(u, w)) {
                    r.fillEdges.push_back({std::min(u, w), std::max(u, w)});
                }
                if (((adj[u] >> w) & 1ULL) == 0) {
                    adj[u] |= 1ULL << w;
                    adj[w] |= 1ULL << u;
                }
            }
        }
        alive[v] = 0;
    }
    std::sort(r.fillEdges.begin(), r.fillEdges.end());
    r.fillEdges.erase(std::unique(r.fillEdges.begin(), r.fillEdges.end()),
                      r.fillEdges.end());
    return r;
}

ExactResult exactOptimalWidth(const Graph& g, int nLimit) {
    int n = g.n;
    if (n > nLimit) {
        throw std::runtime_error(
            "exact enumeration limited to n <= " + std::to_string(nLimit) +
            " (got n=" + std::to_string(n) + ")");
    }
    ExactResult best;
    best.width = std::numeric_limits<int>::max();

    std::vector<int> perm(n);
    for (int i = 0; i < n; ++i) perm[i] = i;
    do {
        ReplayResult r = replayOrder(g, perm);
        ++best.permutationsExamined;
        if (r.width < best.width) {
            best.width = r.width;
            best.order = perm;
        }
    } while (std::next_permutation(perm.begin(), perm.end()));

    if (n == 0) best.width = 0; // empty graph
    return best;
}

int maxCliqueSize(const Graph& g) {
    int n = g.n;
    // Full subset scan (2^n); only usable at the small sizes the exact
    // census operates at. At larger scales return the trivial bound; the
    // DFS prune then relies on the per-state degeneracy lower bound.
    if (n > 30) return 1;
    int best = 0;
    uint64_t total = (1ULL << n) - 1;
    for (uint64_t mask = 1; mask <= total; ++mask) {
        uint64_t row = mask;
        bool clique = true;
        while (row) {
            int u = __builtin_ctzll(row);
            if ((g.adj[u] & mask) != (mask & ~(1ULL << u))) {
                clique = false;
                break;
            }
            row &= row - 1;
        }
        if (clique) best = std::max(best, __builtin_popcountll(mask));
    }
    return best;
}

bool existsOrderWidthLE(std::vector<uint64_t> adj, uint64_t alive, int k) {
    int remaining = __builtin_popcountll(alive);
    if (remaining <= k + 1) return true; // every ordering fits in a k+1 bag

    // Degeneracy lower bound: tw(G) >= max over subgraphs of min degree.
    // Obtained by repeatedly deleting a minimum-degree alive vertex.
    {
        uint64_t a = alive;
        int lb = 0;
        while (a) {
            int minDeg = __builtin_popcountll(a); // upper bound on min degree
            uint64_t r = a;
            while (r) {
                int v = __builtin_ctzll(r);
                minDeg = std::min(minDeg,
                                  __builtin_popcountll(adj[v] & a));
                r &= r - 1;
            }
            lb = std::max(lb, minDeg);
            if (lb > k) return false;
            // Delete one of the minimum-degree vertices.
            r = a;
            while (r) {
                int v = __builtin_ctzll(r);
                if (__builtin_popcountll(adj[v] & a) == minDeg) {
                    a &= ~(1ULL << v);
                    break;
                }
                r &= r - 1;
            }
        }
    }

    // The first eliminated vertex of a width-k ordering has at most k
    // (later) neighbors, i.e. alive degree <= k.
    std::vector<int> candidates;
    uint64_t row = alive;
    while (row) {
        int v = __builtin_ctzll(row);
        if (__builtin_popcountll(adj[v] & alive) <=
            static_cast<size_t>(k)) {
            candidates.push_back(v);
        }
        row &= row - 1;
    }
    if (candidates.empty()) return false;

    for (int v : candidates) {
        std::vector<uint64_t> next = adj;
        uint64_t nb = next[v] & alive;
        uint64_t a = nb;
        while (a) {
            int u = __builtin_ctzll(a);
            uint64_t b = a & (a - 1);
            while (b) {
                int w = __builtin_ctzll(b);
                next[u] |= 1ULL << w;
                next[w] |= 1ULL << u;
                b &= b - 1;
            }
            a &= a - 1;
        }
        uint64_t nextAlive = alive & ~(1ULL << v);
        if (existsOrderWidthLE(std::move(next), nextAlive, k)) return true;
    }
    return false;
}

int fastExactWidth(const Graph& g, int upperBound) {
    int n = g.n;
    if (n == 0) return 0;
    int lower = maxCliqueSize(g) - 1;
    uint64_t alive = n == 64 ? ~0ULL : (1ULL << n) - 1;
    for (int k = lower; k <= upperBound; ++k) {
        if (existsOrderWidthLE(g.adj, alive, k)) return k;
    }
    return upperBound; // sound fallback: heuristic always gives an ordering
}

} // namespace tdw
