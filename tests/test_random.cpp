// Automated tests for the offline dynamic connectivity solver.
//
//  1. RollbackDSU unit tests (incl. randomized rollback vs. recompute model).
//  2. Hand-written cases: query-time boundaries, parallel edges, duplicate
//     delete errors, self loops, out-of-range vertices, empty input.
//  3. Randomized differential test: solve_offline vs. solve_reference
//     (per-query BFS) over thousands of random op sequences, including
//     parallel edges and invalid deletes.
#include <cstdio>
#include <map>
#include <random>
#include <utility>
#include <vector>

#include "rollback_dsu.hpp"
#include "solver.hpp"

using dynconn::Op;
using dynconn::SolveResult;

static int g_checks = 0;
static int g_failures = 0;

static Op add(int u, int v) { return {Op::Type::Add, u, v}; }
static Op del(int u, int v) { return {Op::Type::Del, u, v}; }
static Op qry(int u, int v) { return {Op::Type::Query, u, v}; }

static void check(bool cond, const char* expr, int line) {
    ++g_checks;
    if (!cond) {
        ++g_failures;
        std::printf("FAIL line %d: %s\n", line, expr);
    }
}
#define CHECK(cond) check((cond), #cond, __LINE__)

// ---------------------------------------------------------------- DSU tests

static void test_dsu_basic() {
    RollbackDSU d(6);
    CHECK(!d.connected(0, 1));
    CHECK(d.unite(0, 1));
    CHECK(d.unite(2, 3));
    CHECK(d.connected(0, 1));
    CHECK(!d.connected(0, 2));
    std::size_t cp = d.checkpoint();
    CHECK(d.unite(1, 2));
    CHECK(d.unite(4, 5));
    CHECK(d.connected(0, 3));
    CHECK(d.connected(4, 5));
    CHECK(!d.unite(0, 3));  // already connected -> no-op record
    d.rollback(cp);
    CHECK(d.connected(0, 1));
    CHECK(!d.connected(0, 3));
    CHECK(!d.connected(4, 5));
    d.rollback(0);
    CHECK(!d.connected(0, 1));
}

// Random checkpoints/rollbacks vs. a recompute-from-scratch model.
static void test_dsu_randomized() {
    std::mt19937_64 rng(12345);
    for (int trial = 0; trial < 200; ++trial) {
        const int n = 8;
        RollbackDSU d(n);
        std::vector<std::pair<int, int>> unions;  // active union history
        std::vector<std::size_t> cp_dsu;          // checkpoint stack (dsu side)
        std::vector<std::size_t> cp_model;        // matching model sizes
        for (int step = 0; step < 300; ++step) {
            int action = (int)(rng() % 3);
            if (action == 0 || cp_dsu.empty()) {
                int a = (int)(rng() % n), b = (int)(rng() % n);
                d.unite(a, b);
                unions.push_back({a, b});
            } else if (action == 1) {
                cp_dsu.push_back(d.checkpoint());
                cp_model.push_back(unions.size());
            } else {
                std::size_t idx = rng() % cp_dsu.size();
                d.rollback(cp_dsu[idx]);
                unions.resize(cp_model[idx]);
                cp_dsu.resize(idx);
                cp_model.resize(idx);
            }
            // Verify connectivity of all pairs against the model.
            for (int a = 0; a < n; ++a) {
                for (int b = 0; b < n; ++b) {
                    // model: BFS over `unions`
                    std::vector<std::vector<int>> adj(n);
                    for (auto& e : unions) {
                        adj[e.first].push_back(e.second);
                        adj[e.second].push_back(e.first);
                    }
                    std::vector<char> vis(n, 0);
                    std::vector<int> st = {a};
                    vis[a] = 1;
                    while (!st.empty()) {
                        int x = st.back();
                        st.pop_back();
                        for (int y : adj[x])
                            if (!vis[y]) {
                                vis[y] = 1;
                                st.push_back(y);
                            }
                    }
                    CHECK(d.connected(a, b) == (vis[b] != 0));
                }
            }
        }
    }
}

// ------------------------------------------------------- solver test helper

static void expect(const char* name, long n, const std::vector<Op>& ops, bool ok,
                   long err_idx, const std::vector<bool>& answers) {
    SolveResult off = dynconn::solve_offline(n, ops);
    SolveResult ref = dynconn::solve_reference(n, ops);

    bool off_good = off.ok == ok && off.error_index == (ok ? -1 : err_idx) &&
                    (!ok || off.answers == answers);
    ++g_checks;
    if (!off_good) {
        ++g_failures;
        std::printf("FAIL %s (offline): ok=%d err_idx=%ld answers=[",
                    name, (int)off.ok, off.error_index);
        for (bool a : off.answers) std::printf("%d,", (int)a);
        std::printf("]\n");
    }
    bool agree = ref.ok == off.ok && ref.error_index == off.error_index &&
                 ref.answers == off.answers;
    ++g_checks;
    if (!agree) {
        ++g_failures;
        std::printf("FAIL %s: offline/reference disagree (ref ok=%d err=%ld)\n",
                    name, (int)ref.ok, ref.error_index);
    }
}

// -------------------------------------------------------- hand-written cases

static void test_hand_cases() {
    // Query at index 0, before any edge exists.
    expect("query-first", 2, {qry(0, 1)}, true, -1, {false});
    // Query immediately after an add sees the edge.
    expect("add-then-query", 2, {add(0, 1), qry(0, 1)}, true, -1, {true});
    // Query immediately after a delete no longer sees the edge.
    expect("add-del-query", 2,
           {add(0, 1), qry(0, 1), del(0, 1), qry(0, 1)}, true, -1, {true, false});
    // Add and delete with nothing in between: edge visible nowhere.
    expect("empty-lifetime", 2, {add(0, 1), del(0, 1), qry(0, 1)}, true, -1, {false});
    // Query at the very last index, edge still active (never deleted).
    expect("open-interval-tail", 3,
           {add(0, 1), add(1, 2), del(0, 1), qry(1, 2)}, true, -1, {true});
    // Parallel edges: deleting one instance keeps connectivity.
    expect("parallel-one-left", 2,
           {add(0, 1), add(0, 1), del(0, 1), qry(0, 1)}, true, -1, {true});
    // Parallel edges: deleting both disconnects.
    expect("parallel-both-gone", 2,
           {add(0, 1), add(0, 1), del(0, 1), del(0, 1), qry(0, 1)}, true, -1, {false});
    // Parallel edges with reversed endpoint order are the same edge.
    expect("parallel-reversed", 2,
           {add(0, 1), add(1, 0), del(1, 0), qry(0, 1)}, true, -1, {true});
    // Duplicate delete: error at the offending op index.
    expect("dup-delete", 2, {add(0, 1), del(0, 1), del(0, 1)}, false, 2, {});
    // Delete of an edge that was never added.
    expect("delete-never-added", 3, {del(0, 2)}, false, 0, {});
    // Self loop is harmless; a vertex is always connected to itself.
    expect("self-loop", 2, {add(0, 0), qry(0, 0), qry(1, 1)}, true, -1, {true, true});
    // Vertex id out of range.
    expect("vertex-out-of-range", 2, {add(0, 5)}, false, 0, {});
    expect("vertex-negative", 2, {qry(-1, 0)}, false, 0, {});
    // Empty op sequence.
    expect("empty-ops", 4, {}, true, -1, {});
    // Single vertex.
    expect("single-vertex", 1, {qry(0, 0)}, true, -1, {true});
    // Invalid n.
    expect("n-zero", 0, {qry(0, 0)}, false, -1, {});
    // Transitive connectivity across a path, with a bridge deleted mid-way.
    expect("path-bridge", 4,
           {add(0, 1), add(1, 2), add(2, 3), qry(0, 3),
            del(1, 2), qry(0, 3), qry(0, 1), qry(2, 3)},
           true, -1, {true, false, true, true});
}

// --------------------------------------------------- randomized differential

static void test_randomized(int trials, unsigned long long seed0) {
    for (int t = 0; t < trials; ++t) {
        std::mt19937_64 rng(seed0 + (unsigned long long)t);
        int n = 1 + (int)(rng() % 9);
        int m = (int)(rng() % 71);
        std::vector<Op> ops;
        std::map<std::pair<int, int>, int> active;
        auto rand_vertex = [&]() { return (int)(rng() % n); };
        for (int i = 0; i < m; ++i) {
            double x = (double)(rng() % 1000) / 1000.0;
            if (x < 0.45 || active.empty()) {
                int u = rand_vertex();
                int v = rand_vertex();
                if (rng() % 16 == 0) v = u;  // occasional self loop
                ops.push_back(add(u, v));
                int a = std::min(u, v), b = std::max(u, v);
                active[{a, b}]++;
            } else if (x < 0.75) {
                if (rng() % 100 < 85) {
                    // valid delete of a random active instance
                    auto it = active.begin();
                    std::advance(it, rng() % active.size());
                    ops.push_back(del(it->first.first, it->first.second));
                    if (--(it->second) == 0) active.erase(it);
                } else {
                    // possibly invalid delete (duplicate / never added)
                    ops.push_back(del(rand_vertex(), rand_vertex()));
                }
            } else {
                ops.push_back(qry(rand_vertex(), rand_vertex()));
            }
        }
        SolveResult off = dynconn::solve_offline(n, ops);
        SolveResult ref = dynconn::solve_reference(n, ops);
        bool agree = off.ok == ref.ok && off.error_index == ref.error_index &&
                     off.answers == ref.answers;
        ++g_checks;
        if (!agree) {
            ++g_failures;
            std::printf("FAIL random trial %d (n=%d, m=%d): offline ok=%d err=%ld, "
                        "reference ok=%d err=%ld\n",
                        t, n, m, (int)off.ok, off.error_index, (int)ref.ok,
                        ref.error_index);
            std::printf("  ops:");
            for (const Op& op : ops) {
                const char* tn = op.type == Op::Type::Add    ? "add"
                                 : op.type == Op::Type::Del  ? "del"
                                                             : "qry";
                std::printf(" %s(%d,%d)", tn, op.u, op.v);
            }
            std::printf("\n");
            if (g_failures > 10) return;
        }
    }
}

int main() {
    test_dsu_basic();
    test_dsu_randomized();
    test_hand_cases();
    test_randomized(3000, 20260925ULL);
    std::printf("test_random: %d checks, %d failures\n", g_checks, g_failures);
    return g_failures == 0 ? 0 : 1;
}
