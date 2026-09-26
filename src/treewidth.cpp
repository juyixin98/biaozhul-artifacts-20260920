#include "treewidth.hpp"

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <limits>
#include <map>
#include <utility>

namespace {

using Clock = std::chrono::steady_clock;

double msSince(Clock::time_point t0) {
    return std::chrono::duration<double, std::milli>(Clock::now() - t0).count();
}

// Mutable elimination workspace over a simple undirected graph.
struct WorkGraph {
    int n;
    std::vector<std::vector<char>> adj;
    std::vector<char> alive;

    explicit WorkGraph(const Graph& g) : n(g.n()), adj(g.adj), alive(n, 1) {}

    int degree(int v) const {
        int d = 0;
        for (int u = 0; u < n; ++u)
            if (u != v && alive[u] && adj[v][u]) ++d;
        return d;
    }

    std::vector<int> neighbors(int v) const {
        std::vector<int> out;
        for (int u = 0; u < n; ++u)
            if (u != v && alive[u] && adj[v][u]) out.push_back(u);
        return out;
    }

    // Number of missing edges among pairs of v's current neighbors.
    int fillCount(int v) const {
        int count = 0;
        for (int a = 0; a < n; ++a) {
            if (!alive[a] || a == v || !adj[v][a]) continue;
            for (int b = a + 1; b < n; ++b) {
                if (!alive[b] || b == v || !adj[v][b]) continue;
                if (!adj[a][b]) ++count;
            }
        }
        return count;
    }

    // Makes v's remaining neighborhood a clique; returns added fill edges.
    std::vector<std::pair<int, int>> eliminate(int v) {
        std::vector<int> nb = neighbors(v);
        std::vector<std::pair<int, int>> added;
        for (size_t i = 0; i < nb.size(); ++i) {
            for (size_t j = i + 1; j < nb.size(); ++j) {
                int a = nb[i], b = nb[j];
                if (!adj[a][b]) {
                    adj[a][b] = adj[b][a] = 1;
                    added.emplace_back(a, b);
                }
            }
        }
        alive[v] = 0;
        return added;
    }
};

EliminationResult runHeuristic(const Graph& g, bool minFill) {
    auto t0 = Clock::now();
    WorkGraph w(g);
    EliminationResult r;
    r.heuristic = minFill ? "min-fill" : "min-degree";

    int remaining = g.n();
    int maxDegree = 0;
    while (remaining > 0) {
        int best = -1;
        int bestFill = std::numeric_limits<int>::max();
        int bestDegree = std::numeric_limits<int>::max();
        for (int v = 0; v < g.n(); ++v) {
            if (!w.alive[v]) continue;
            int deg = w.degree(v);
            int fill = minFill ? w.fillCount(v) : deg;
            int tieDeg = minFill ? deg : 0;
            if (fill < bestFill ||
                (fill == bestFill && tieDeg < bestDegree) ||
                (fill == bestFill && tieDeg == bestDegree &&
                 (best == -1 || v < best))) {
                best = v;
                bestFill = fill;
                bestDegree = tieDeg;
            }
        }

        std::vector<int> nb = w.neighbors(best);
        ElimStep step;
        step.position = static_cast<int>(r.order.size());
        step.vertex = best;
        step.degreeAtElimination = static_cast<int>(nb.size());
        step.bag.push_back(best);
        for (int u : nb) {
            if (u != best) step.bag.push_back(u);
        }
        std::sort(step.bag.begin(), step.bag.end());
        step.addedFill = w.eliminate(best);
        for (auto& e : step.addedFill)
            if (e.first > e.second) std::swap(e.first, e.second);
        std::sort(step.addedFill.begin(), step.addedFill.end());
        r.totalFill += static_cast<long long>(step.addedFill.size());
        maxDegree = std::max(maxDegree, step.degreeAtElimination);
        r.order.push_back(best);
        r.steps.push_back(std::move(step));
        --remaining;
    }
    r.width = g.n() == 0 ? -1 : maxDegree;
    r.elapsedMs = msSince(t0);
    return r;
}

// Bit position of an undirected edge over original indices (n <= 11).
uint64_t edgeBit(int u, int v, int n) {
    if (u > v) std::swap(u, v);
    int index = 0;
    for (int a = 0; a < u; ++a) index += n - 1 - a;
    index += v - u - 1;
    return uint64_t{1} << index;
}

struct ExactSolver {
    int n;
    std::vector<uint64_t> initialAdj;  // neighbor mask per vertex
    std::map<std::pair<uint64_t, uint64_t>, int> memo;
    long long states = 0;
    long long nodes = 0;

    ExactSolver(const Graph& g) : n(g.n()), initialAdj(n, 0) {
        for (int v = 0; v < n; ++v)
            for (int u = 0; u < n; ++u)
                if (g.adj[v][u]) initialAdj[v] |= uint64_t{1} << u;
    }

    // Exact DP over (remaining set S, induced fill-edge set E).
    // Width(S, E) = min over v in S of max(deg_E_S(v), Width(S\\v, E+v-fill)).
    // The only pruning is the sound local bound `deg >= best`; the first
    // candidate is always explored, so every memoized value is the state's
    // true optimum (a global-best cut could leave a state with no explored
    // candidate and poison the memo with a sentinel).
    int solve(uint64_t s, const std::vector<uint64_t>& adjMasks) {
        if (s == 0) return 0;
        uint64_t edgeSet = 0;
        for (uint64_t m = s; m; m &= m - 1) {
            int v = __builtin_ctzll(m);
            uint64_t nb = adjMasks[v] & s;
            for (uint64_t t = nb; t; t &= t - 1) {
                int u = __builtin_ctzll(t);
                if (u > v) edgeSet |= edgeBit(u, v, n);
            }
        }
        auto key = std::make_pair(s, edgeSet);
        auto it = memo.find(key);
        if (it != memo.end()) return it->second;
        ++states;

        int best = n;
        for (uint64_t m = s; m; m &= m - 1) {
            int v = __builtin_ctzll(m);
            ++nodes;
            uint64_t nb = adjMasks[v] & s;
            int deg = __builtin_popcountll(nb);
            if (deg >= best) continue;  // max(deg, sub) >= deg cannot improve
            std::vector<uint64_t> next = adjMasks;
            for (uint64_t a = nb; a; a &= a - 1) {
                int x = __builtin_ctzll(a);
                next[x] |= nb & ~(uint64_t{1} << x);
            }
            int sub = solve(s & ~(uint64_t{1} << v), next);
            best = std::min(best, std::max(deg, sub));
        }
        memo[key] = best;
        return best;
    }

    // Reconstruct an order attaining the optimum, using the memoized solve.
    std::vector<int> reconstruct(uint64_t s,
                                 const std::vector<uint64_t>& adjMasks) {
        if (s == 0) return {};
        int target = solve(s, adjMasks);
        for (uint64_t m = s; m; m &= m - 1) {
            int v = __builtin_ctzll(m);
            uint64_t nb = adjMasks[v] & s;
            int deg = __builtin_popcountll(nb);
            std::vector<uint64_t> next = adjMasks;
            for (uint64_t a = nb; a; a &= a - 1) {
                int x = __builtin_ctzll(a);
                next[x] |= nb & ~(uint64_t{1} << x);
            }
            uint64_t rest = s & ~(uint64_t{1} << v);
            if (std::max(deg, solve(rest, next)) == target) {
                std::vector<int> tail = reconstruct(rest, next);
                std::vector<int> out;
                out.reserve(1 + tail.size());
                out.push_back(v);
                for (int x : tail) out.push_back(x);
                return out;
            }
        }
        return {};  // unreachable
    }
};

int widthOfOrder(const Graph& g, const std::vector<int>& order) {
    int n = g.n();
    if (n == 0) return -1;
    std::vector<std::vector<char>> adj = g.adj;
    std::vector<char> alive(n, 1);
    int width = 0;
    for (int v : order) {
        int deg = 0;
        std::vector<int> nb;
        for (int u = 0; u < n; ++u)
            if (u != v && alive[u] && adj[v][u]) { ++deg; nb.push_back(u); }
        width = std::max(width, deg);
        for (size_t i = 0; i < nb.size(); ++i)
            for (size_t j = i + 1; j < nb.size(); ++j)
                adj[nb[i]][nb[j]] = adj[nb[j]][nb[i]] = 1;
        alive[v] = 0;
    }
    return width;
}

}  // namespace

EliminationResult minFillOrder(const Graph& g) { return runHeuristic(g, true); }

EliminationResult minDegreeOrder(const Graph& g) {
    return runHeuristic(g, false);
}

int eliminationWidth(const Graph& g, const std::vector<int>& order) {
    return widthOfOrder(g, order);
}

TreeDecomposition buildTreeDecomposition(const Graph& g,
                                         const std::vector<int>& order) {
    TreeDecomposition td;
    int n = g.n();
    if (n == 0) {
        td.width = -1;  // width of the empty graph's decomposition
        return td;
    }

    // Replay elimination to learn each vertex's "later" (filled) neighbors.
    WorkGraph w(g);
    std::vector<std::vector<int>> later(n);
    std::vector<int> position(n, -1);
    for (int i = 0; i < n; ++i) position[order[i]] = i;

    int width = 0;
    for (int v : order) {
        later[v] = w.neighbors(v);
        width = std::max(width, static_cast<int>(later[v].size()));
        w.eliminate(v);
    }

    td.bags.resize(n);
    for (int i = 0; i < n; ++i) {
        int v = order[i];
        TDBag& bag = td.bags[i];
        bag.id = i;
        bag.vertices.push_back(v);
        for (int u : later[v]) bag.vertices.push_back(u);
        std::sort(bag.vertices.begin(), bag.vertices.end());

        int parentVertex = -1;
        int parentPos = std::numeric_limits<int>::max();
        for (int u : later[v]) {
            if (position[u] < parentPos) {
                parentPos = position[u];
                parentVertex = u;
            }
        }
        bag.parent = parentVertex < 0 ? -1 : position[parentVertex];
    }

    // Tree edges from parent pointers; collect component roots.
    std::vector<int> roots;
    for (const TDBag& bag : td.bags) {
        if (bag.parent < 0) {
            roots.push_back(bag.id);
        } else {
            td.treeEdges.emplace_back(bag.id, bag.parent);
        }
    }
    // Join component trees into a single tree (a path through their roots).
    for (size_t i = 1; i < roots.size(); ++i)
        td.rootJoinEdges.emplace_back(roots[i - 1], roots[i]);

    td.roots = static_cast<int>(roots.size());
    td.width = width;
    return td;
}

ExactResult exactOptimal(const Graph& g, int limit) {
    auto t0 = Clock::now();
    ExactResult er;
    int n = g.n();
    if (n > MAX_EXACT_N_HARD || limit > MAX_EXACT_N_HARD) {
        er.hitHardLimit = true;
        er.elapsedMs = msSince(t0);
        return er;
    }
    if (n > limit) {
        er.feasible = false;
        er.elapsedMs = msSince(t0);
        return er;
    }

    ExactSolver solver(g);
    uint64_t all = n == 64 ? ~uint64_t{0} : (uint64_t{1} << n) - 1;
    int w = solver.solve(all, solver.initialAdj);

    std::vector<int> order = solver.reconstruct(all, solver.initialAdj);

    er.feasible = true;
    er.width = w;
    er.order = std::move(order);
    er.memoStates = solver.states;
    er.searchNodes = solver.nodes;
    er.elapsedMs = msSince(t0);
    return er;
}

NaiveResult naiveOptimal(const Graph& g) {
    auto t0 = Clock::now();
    NaiveResult nr;
    int n = g.n();
    std::vector<int> order(n);
    for (int i = 0; i < n; ++i) order[i] = i;

    int best = n;
    std::vector<int> bestOrder;
    long long count = 0;
    do {
        ++count;
        int w = widthOfOrder(g, order);
        if (w < best) {
            best = w;
            bestOrder = order;
        }
    } while (std::next_permutation(order.begin(), order.end()));

    nr.width = n == 0 ? -1 : best;
    nr.order = std::move(bestOrder);
    nr.permutations = count;
    nr.elapsedMs = msSince(t0);
    return nr;
}
