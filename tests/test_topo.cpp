// Differential & scenario tests for the incremental topological sorter.
//
// Strategy: drive IncrementalTopo and NaiveTopo with identical edge streams,
// compare accept/reject/duplicate decisions after every insertion, and verify
// the incremental order with an independent validator. Covers:
//   * self-loops
//   * reverse-order long chain (worst-case reorder stream)
//   * isolated vertices
//   * duplicate edges
//   * explicit cycle witness validation
//   * failed-insertion leaves the graph untouched
//   * randomized differential fuzzing vs the naive reference
//   * actual visited-node statistics
//
// Exit code 0 iff every check passed.
#include <algorithm>
#include <cstdio>
#include <functional>
#include <numeric>
#include <string>
#include <vector>

#include "../src/naive_topo.hpp"
#include "../src/topo.hpp"

using namespace topo;

namespace {

int g_failures = 0;
int g_checks = 0;

void check(bool cond, const std::string& what) {
    ++g_checks;
    if (!cond) {
        ++g_failures;
        std::printf("  FAIL: %s\n", what.c_str());
    }
}

// Tiny deterministic LCG so fuzz trials are reproducible.
struct Rng {
    unsigned long long state;
    explicit Rng(unsigned long long seed) : state(seed ? seed : 0x9e3779b97f4a7c15ULL) {}
    unsigned next() {
        state = state * 6364136223846793005ULL + 1442695040888963407ULL;
        return static_cast<unsigned>(state >> 33);
    }
    int below(int n) { return n <= 0 ? 0 : static_cast<int>(next() % static_cast<unsigned>(n)); }
};

// Verify a returned cycle witness: path must be [u, v, ..., u]; every
// consecutive pair after the rejected candidate edge must already exist.
bool validCycleWitness(const IncrementalTopo& t, int u, int v,
                       const std::vector<int>& path) {
    if (path.size() < 3) return false;
    if (path.front() != u || path.back() != u) return false;
    if (path[1] != v) return false;
    for (size_t i = 1; i + 1 < path.size(); ++i) {
        if (!t.hasEdge(path[i], path[i + 1])) return false;
    }
    // All interior vertices distinct (simple cycle).
    std::vector<int> interior(path.begin() + 1, path.end() - 1);
    std::sort(interior.begin(), interior.end());
    return std::adjacent_find(interior.begin(), interior.end()) == interior.end();
}

// ---------------------------------------------------------------------------
// Scenario 1: self loops are always rejected and change nothing.
// ---------------------------------------------------------------------------
void testSelfLoops() {
    std::printf("[scenario] self loops\n");
    IncrementalTopo t(20);
    std::vector<int> before = t.order();
    for (int i = 0; i < 20; ++i) {
        auto r = t.insertEdge(i, i);
        check(!r.accepted && r.cycle, "self-loop rejected");
        check(r.cyclePath == std::vector<int>({i, i}), "self-loop witness [i,i]");
        check(t.order() == before, "order unchanged after self-loop");
        check(t.edgeCount() == 0, "edge count unchanged after self-loop");
    }
}

// ---------------------------------------------------------------------------
// Scenario 2: insert a long chain in reverse order: n-1 -> n-2 -> ... -> 0.
// Every edge is "backwards" at insertion time, forcing the reorder path.
// ---------------------------------------------------------------------------
void testReverseChain(int n) {
    std::printf("[scenario] reverse-order long chain n=%d\n", n);
    IncrementalTopo t(n);
    long long totalVisited = 0;
    for (int i = n - 1; i >= 1; --i) {
        auto r = t.insertEdge(i, i - 1);
        check(r.accepted && !r.cycle, "reverse chain edge accepted");
        check(r.reordered, "reverse chain edge caused a reorder");
        totalVisited += r.visited;
    }
    std::vector<int> expected(n);
    std::iota(expected.begin(), expected.end(), 0);
    std::reverse(expected.begin(), expected.end());
    check(t.order() == expected, "final order is n-1,...,0");

    Graph g(n);
    for (int i = n - 1; i >= 1; --i) g.addEdge(i, i - 1);
    check(validateOrder(g, t.order()).empty(), "order satisfies every chain edge");

    std::printf("           total visited nodes: %lld over %d reordered inserts"
                " (avg %.2f)\n",
                totalVisited, n - 1,
                static_cast<double>(totalVisited) / (n - 1));
}

// ---------------------------------------------------------------------------
// Scenario 3: isolated vertices remain present exactly once in the order.
// ---------------------------------------------------------------------------
void testIsolatedVertices() {
    std::printf("[scenario] isolated vertices\n");
    const int n = 50;
    IncrementalTopo t(n);
    auto r1 = t.insertEdge(10, 40);
    auto r2 = t.insertEdge(40, 5);
    check(r1.accepted && r2.accepted, "two edges accepted");
    const auto& o = t.order();
    check(static_cast<int>(o.size()) == n, "order still lists all vertices");
    std::vector<int> sorted = o;
    std::sort(sorted.begin(), sorted.end());
    bool permutation = true;
    for (int i = 0; i < n; ++i)
        if (sorted[i] != i) permutation = false;
    check(permutation, "order is a permutation of 0..n-1 (isolated kept)");
    check(t.positionOf(10) < t.positionOf(40), "10 before 40");
    check(t.positionOf(40) < t.positionOf(5), "40 before 5");
}

// ---------------------------------------------------------------------------
// Scenario 4: duplicate edges are idempotent.
// ---------------------------------------------------------------------------
void testDuplicates() {
    std::printf("[scenario] duplicate edges\n");
    IncrementalTopo t(6);
    auto a = t.insertEdge(5, 1); // backwards -> reorder
    check(a.accepted && !a.duplicate, "first insert succeeds");
    std::vector<int> before = t.order();
    for (int k = 0; k < 10; ++k) {
        auto r = t.insertEdge(5, 1);
        check(r.accepted && r.duplicate, "duplicate reported as duplicate");
        check(!r.cycle && !r.reordered && r.visited == 0,
              "duplicate does no search work");
        check(t.order() == before, "duplicate leaves order untouched");
    }
    check(t.edgeCount() == 1, "duplicates counted once");
    // Duplicate of a forward edge too.
    auto b = t.insertEdge(1, 2);
    auto c = t.insertEdge(1, 2);
    check(b.accepted && c.accepted && c.duplicate && t.edgeCount() == 2,
          "forward duplicate handled");
}

// ---------------------------------------------------------------------------
// Scenario 5: an explicit cycle, witness path and untouched graph.
// ---------------------------------------------------------------------------
void testExplicitCycle() {
    std::printf("[scenario] explicit cycle + failed insert atomicity\n");
    IncrementalTopo t(4);
    for (auto [u, v] : {std::pair<int, int>{0, 1}, {1, 2}, {2, 3}}) {
        auto r = t.insertEdge(u, v);
        check(r.accepted, "chain edge accepted");
    }
    std::vector<int> before = t.order();
    auto r = t.insertEdge(3, 0);
    check(!r.accepted && r.cycle, "closing edge rejected as cycle");
    check(validCycleWitness(t, 3, 0, r.cyclePath), "cycle witness verified");
    check(r.cyclePath == std::vector<int>({3, 0, 1, 2, 3}),
          "witness is exactly 3,0,1,2,3");
    check(t.order() == before, "order unchanged after rejected insert");
    check(!t.hasEdge(3, 0), "rejected edge absent from graph");
    check(t.edgeCount() == 3, "edge count unchanged after rejection");

    // Graph still usable after rejection; a legal edge still works.
    auto ok = t.insertEdge(0, 2);
    check(ok.accepted, "legal edge accepted after a rejection");
}

// ---------------------------------------------------------------------------
// Randomized differential fuzzing vs the naive reference.
// ---------------------------------------------------------------------------
struct FuzzStats {
    long long inserts = 0;
    long long accepted = 0;
    long long duplicates = 0;
    long long cycles = 0;
    long long reordered = 0;
    long long visited = 0;
};

void fuzzOnce(int n, int attempts, unsigned seed, FuzzStats& agg) {
    IncrementalTopo inc(n);
    NaiveTopo naive(n);
    Rng rng(seed);

    for (int k = 0; k < attempts; ++k) {
        int u = rng.below(n);
        int v = rng.below(n);
        std::vector<int> before = inc.order();

        auto ri = inc.insertEdge(u, v);
        auto rn = naive.insertEdge(u, v);
        ++agg.inserts;

        check(ri.accepted == rn.accepted,
              "accept decision matches naive reference");
        check(ri.cycle == rn.cycle, "cycle decision matches naive reference");
        check(ri.duplicate == rn.duplicate,
              "duplicate decision matches naive reference");
        check(inc.edgeCount() == naive.edgeCount(), "edge counts agree");

        if (ri.duplicate) {
            ++agg.duplicates;
            continue;
        }
        if (ri.cycle) {
            ++agg.cycles;
            check(inc.order() == before, "rejected insert left order unchanged");
            check(!inc.hasEdge(u, v), "rejected edge not stored");
            if (u == v) {
                check(ri.cyclePath == std::vector<int>({u, u}),
                      "self-loop witness is [u,u]");
            } else {
                check(validCycleWitness(inc, u, v, ri.cyclePath),
                      "returned cycle path is a real closed path");
            }
            continue;
        }

        ++agg.accepted;
        if (ri.reordered) {
            ++agg.reordered;
            agg.visited += ri.visited;
        }
        std::string err = validateOrder(naive.graph(), inc.order());
        check(err.empty(), std::string("incremental order valid: ") + err);
    }

    // Final cross-check: incremental order vs a fresh full Kahn run.
    std::vector<int> full;
    check(kahnOrder(naive.graph(), full), "reference graph stays acyclic");
    check(validateOrder(naive.graph(), inc.order()).empty(),
          "final incremental order passes full validation");
}

void testRandomDifferential() {
    std::printf("[scenario] randomized differential fuzzing\n");
    FuzzStats agg;
    // Exhaustive small graphs (all vertex-pair draws incl. self loops).
    for (unsigned seed = 1; seed <= 400; ++seed) {
        int n = 1 + static_cast<int>(seed % 12);
        fuzzOnce(n, n * n * 2, seed * 7919u + 13u, agg);
    }
    // Medium sparse and dense trials.
    fuzzOnce(200, 2000, 12345, agg);
    fuzzOnce(500, 4000, 67890, agg);
    fuzzOnce(100, 9000, 424242, agg);

    std::printf("           inserts=%lld accepted=%lld duplicates=%lld cycles=%lld\n",
                agg.inserts, agg.accepted, agg.duplicates, agg.cycles);
    std::printf("           reordered inserts=%lld, total visited=%lld, avg=%.2f\n",
                agg.reordered, agg.visited,
                agg.reordered ? static_cast<double>(agg.visited) / agg.reordered : 0.0);
}

// ---------------------------------------------------------------------------
// Visited-node accounting on a medium reverse chain + forward noise.
// ---------------------------------------------------------------------------
void testVisitedStats() {
    std::printf("[scenario] visited-node statistics (n=2000 mixed stream)\n");
    const int n = 2000;
    IncrementalTopo t(n);
    Graph ref(n);
    Rng rng(987654321u);
    long long trivial = 0, reordered = 0, totalVisited = 0;
    long long maxVisited = 0;
    for (int k = 0; k < 3000; ++k) {
        int u, v;
        if ((k % 3) == 0) { // backwards-biased edge
            u = rng.below(n);
            v = rng.below(n);
        } else {
            int a = rng.below(n), b = rng.below(n);
            u = std::max(a, b);
            v = std::min(a, b);
        }
        if (u == v) continue;
        auto r = t.insertEdge(u, v);
        if (r.duplicate || r.cycle) continue;
        ref.addEdge(u, v);
        if (r.reordered) {
            ++reordered;
            totalVisited += r.visited;
            maxVisited = std::max(maxVisited, r.visited);
        } else {
            ++trivial;
        }
    }
    check(validateOrder(ref, t.order()).empty(),
          "mixed-stream final order satisfies every edge");
    std::printf("           trivial=%lld reordered=%lld visited(total/avg/max)"
                "=%lld/%.2f/%lld\n",
                trivial, reordered, totalVisited,
                reordered ? static_cast<double>(totalVisited) / reordered : 0.0,
                maxVisited);
}

} // namespace

int main() {
    testSelfLoops();
    testReverseChain(500);
    testIsolatedVertices();
    testDuplicates();
    testExplicitCycle();
    testRandomDifferential();
    testVisitedStats();

    std::printf("\nchecks=%d failures=%d\n", g_checks, g_failures);
    if (g_failures != 0) {
        std::printf("RESULT: FAIL\n");
        return 1;
    }
    std::printf("RESULT: PASS\n");
    return 0;
}
