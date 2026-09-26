// Self-contained unit tests for the incremental topological-order engine.
// No external test framework; a tiny assertion macro keeps the build
// dependency-free. Build via `make` / `make test`.

#include <algorithm>
#include <cstdint>
#include <cstdio>
#include <functional>
#include <random>
#include <string>
#include <unordered_set>
#include <vector>

#include "topo.hpp"

namespace {

using topo::IncrementalTopo;
using topo::InsertResult;

int g_failures = 0;
int g_checks = 0;

#define CHECK(cond)                                                          \
    do {                                                                     \
        ++g_checks;                                                          \
        if (!(cond)) {                                                       \
            ++g_failures;                                                    \
            std::printf("FAIL %s:%d  %s\n", __FILE__, __LINE__, #cond);      \
        }                                                                    \
    } while (0)

bool orderRespectsEdges(const std::vector<std::unordered_set<int>>& succ,
                        const std::vector<int>& present,
                        const std::vector<int>& pos) {
    for (int x : present) {
        for (int y : succ[x]) {
            if (pos[x] >= pos[y]) return false;
        }
    }
    return true;
}

// Naive reference: full Kahn recomputation on the model graph.
bool kahnIsAcyclic(int n,
                   const std::vector<std::unordered_set<int>>& succ,
                   const std::vector<int>& present) {
    std::vector<int> indeg(n, 0);
    for (int x : present) {
        for (int y : succ[x]) ++indeg[y];
    }
    std::vector<int> q;
    for (int x : present) {
        if (indeg[x] == 0) q.push_back(x);
    }
    int seen = 0;
    while (!q.empty()) {
        int x = q.back();
        q.pop_back();
        ++seen;
        for (int y : succ[x]) {
            if (--indeg[y] == 0) q.push_back(y);
        }
    }
    return seen == static_cast<int>(present.size());
}

void syncPositions(const IncrementalTopo& g, std::vector<int>& pos) {
    const auto& order = g.order();
    pos.assign(pos.size(), -1);
    for (size_t i = 0; i < order.size(); ++i) pos[order[i]] = static_cast<int>(i);
}

void testBasicForwardInsert() {
    IncrementalTopo g;
    CHECK(g.insertEdge(1, 2).ok);
    CHECK(g.insertEdge(2, 3).ok);
    CHECK(g.insertEdge(1, 3).ok);  // already ordered, no search
    auto rep = g.verify();
    CHECK(rep.valid);
    CHECK(rep.acyclic);
    CHECK(rep.permutation);
    const auto& o = g.order();
    CHECK(std::find(o.begin(), o.end(), 1) < std::find(o.begin(), o.end(), 2));
}

void testReorderTriggersSearch() {
    IncrementalTopo g;
    // Force initial order [3,2,1] via node insertion, then add 1->2->3.
    g.addNode(3);
    g.addNode(2);
    g.addNode(1);
    InsertResult r1 = g.insertEdge(1, 2);  // 1 after 2: must reorder
    CHECK(r1.ok);
    CHECK(!r1.cycle);
    CHECK(r1.visited > 0);
    InsertResult r2 = g.insertEdge(2, 3);
    CHECK(r2.ok);
    auto rep = g.verify();
    CHECK(rep.valid);
    const auto& o = g.order();
    CHECK(std::find(o.begin(), o.end(), 1) < std::find(o.begin(), o.end(), 2));
    CHECK(std::find(o.begin(), o.end(), 2) < std::find(o.begin(), o.end(), 3));
}

void testSelfLoop() {
    IncrementalTopo g;
    InsertResult r = g.insertEdge(5, 5);
    CHECK(r.cycle);
    CHECK(!r.ok);
    CHECK(r.cyclePath.size() == 1 && r.cyclePath[0] == 5);
    CHECK(g.numNodes() == 0);  // failed self-loop must not even create the node
    InsertResult again = g.insertEdge(5, 5);
    CHECK(again.cycle);
}

void testDuplicateEdges() {
    IncrementalTopo g;
    CHECK(g.insertEdge(1, 2).ok);
    InsertResult r2 = g.insertEdge(1, 2);
    CHECK(r2.ok);
    CHECK(r2.duplicated);
    CHECK(g.numEdges() == 1);
    CHECK(g.numNodes() == 2);
    // Duplicate of an edge after reorders still works.
    CHECK(g.insertEdge(2, 3).ok);
    CHECK(g.insertEdge(1, 2).duplicated);
    CHECK(g.numEdges() == 2);
}

void testCycleRejectedAndGraphUnchanged() {
    IncrementalTopo g;
    // 1->2->3->1 closes a cycle.
    g.addNode(1);
    g.addNode(2);
    g.addNode(3);
    CHECK(g.insertEdge(1, 2).ok);
    CHECK(g.insertEdge(2, 3).ok);
    int64_t edgesBefore = g.numEdges();
    InsertResult r = g.insertEdge(3, 1);
    CHECK(r.cycle);
    CHECK(!r.ok);
    // Reported existing path runs from target v=1 back to source u=3; the
    // rejected edge 3->1 closes it.
    CHECK(r.cyclePath.size() >= 2);
    CHECK(r.cyclePath.front() == 1);
    CHECK(r.cyclePath.back() == 3);
    // The rejected edge must not be stored.
    CHECK(g.numEdges() == edgesBefore);
    auto rep = g.verify();
    CHECK(rep.valid);
    CHECK(rep.acyclic);
}

void testCyclePathEdgesExist() {
    // Build graph where the cycle uses non-trivial paths and validate that
    // each consecutive pair reported is genuinely an edge in the model.
    IncrementalTopo g;
    std::vector<std::pair<int, int>> edges = {
        {1, 2}, {2, 4}, {4, 5}, {1, 3}, {3, 5}};
    for (auto [u, v] : edges) CHECK(g.insertEdge(u, v).ok);
    // Close 5 -> 1: existing path 1 -> 2 -> 4 -> 5 makes the cycle.
    InsertResult r = g.insertEdge(5, 1);
    CHECK(r.cycle);
    CHECK(r.cyclePath.front() == 1);
    CHECK(r.cyclePath.back() == 5);
    // Every pair path[i]->path[i+1] must be among stored edges.
    std::unordered_set<long> have;
    for (auto [u, v] : edges) have.insert(static_cast<long>(u) * 1000003LL + v);
    for (size_t i = 0; i + 1 < r.cyclePath.size(); ++i) {
        long key = static_cast<long>(r.cyclePath[i]) * 1000003LL +
                   r.cyclePath[i + 1];
        CHECK(have.count(key) == 1);
    }
    CHECK(g.numEdges() == static_cast<int64_t>(edges.size()));
}

void testIsolatedNodes() {
    IncrementalTopo g;
    g.addNode(10);
    g.addNode(11);
    g.addNode(12);
    CHECK(g.insertEdge(1, 2).ok);
    auto rep = g.verify();
    CHECK(rep.valid);
    CHECK(rep.permutation);
    CHECK(g.numNodes() == 5);
    // All isolated nodes present exactly once.
    const auto& o = g.order();
    for (int x : {10, 11, 12, 1, 2}) {
        CHECK(std::count(o.begin(), o.end(), x) == 1);
    }
}

void testReverseLongChain() {
    // Insert nodes n-1...0 then edges 0->1->...->n-1 in forward numeric
    // order while positions are reversed: every edge triggers a reorder.
    const int n = 200;
    IncrementalTopo g;
    for (int i = n - 1; i >= 0; --i) g.addNode(i);
    int64_t totalVisited = 0;
    for (int i = 0; i + 1 < n; ++i) {
        InsertResult r = g.insertEdge(i, i + 1);
        CHECK(r.ok);
        CHECK(!r.cycle);
        totalVisited += r.visited;
    }
    auto rep = g.verify();
    CHECK(rep.valid);
    CHECK(rep.acyclic);
    const auto& o = g.order();
    for (int i = 0; i + 1 < n; ++i) {
        CHECK(std::find(o.begin(), o.end(), i) <
              std::find(o.begin(), o.end(), i + 1));
    }
    CHECK(totalVisited > 0);
    // Backward edge must be rejected as a cycle; existing path is 0..n-1.
    InsertResult bad = g.insertEdge(n - 1, 0);
    CHECK(bad.cycle);
    CHECK(bad.cyclePath.front() == 0);
    CHECK(bad.cyclePath.back() == n - 1);
}

// Differential fuzz test: random edge insertions compared against an
// independent model graph + naive Kahn, after every single insertion.
void fuzzAgainstNaive(uint32_t seed, int n, int trials) {
    std::mt19937 rng(seed);
    std::uniform_int_distribution<int> pick(0, n - 1);

    IncrementalTopo g(n);
    std::vector<std::unordered_set<int>> succ(n);
    std::vector<int> present;
    std::vector<char> isPresent(n, 0);
    std::vector<int> pos(n, -1);

    // Pre-create some isolated vertices.
    for (int i = 0; i < n; ++i) {
        g.addNode(i);
        present.push_back(i);
        isPresent[i] = 1;
    }
    syncPositions(g, pos);

    int accepted = 0;
    int rejectedCycles = 0;
    int duplicates = 0;

    for (int t = 0; t < trials; ++t) {
        int u = pick(rng);
        int v = pick(rng);

        bool modelHas = succ[u].count(v) != 0;
        // Determine reference outcome before applying: simulate insertion in
        // model and run Kahn from scratch.
        bool wouldCycle = false;
        if (u == v) {
            wouldCycle = true;
        } else if (modelHas) {
            wouldCycle = false;  // duplicate stays accepted
        } else {
            succ[u].insert(v);
            wouldCycle = !kahnIsAcyclic(n, succ, present);
            succ[u].erase(v);
        }

        InsertResult r = g.insertEdge(u, v);

        if (wouldCycle) {
            CHECK(r.cycle);
            CHECK(!r.ok);
            ++rejectedCycles;
            // Failed insertion changes nothing: model is not modified.
        } else {
            CHECK(r.ok);
            CHECK(!r.cycle);
            if (modelHas) {
                CHECK(r.duplicated);
                ++duplicates;
            } else {
                CHECK(!r.duplicated);
                succ[u].insert(v);
                ++accepted;
            }
        }
        syncPositions(g, pos);
        CHECK(orderRespectsEdges(succ, present, pos));
        auto rep = g.verify();
        CHECK(rep.valid);
        // The maintained graph is always acyclic: cyclic edges are never stored.
        CHECK(rep.acyclic);
        CHECK(g.numEdges() == [&] {
            int64_t c = 0;
            for (int x = 0; x < n; ++x) c += static_cast<int64_t>(succ[x].size());
            return c;
        }());
    }
    std::printf("  fuzz seed=%u accepted=%d dup=%d cycles=%d total_visited=%lld\n",
                seed, accepted, duplicates, rejectedCycles,
                static_cast<long long>(g.totalVisited()));
}

void testScaleLimit() {
    // Low caps prove rejected insertions/additions mutate nothing.
    IncrementalTopo g(/*reserve=*/4, /*maxNodes=*/3, /*maxEdges=*/2);
    CHECK(g.insertEdge(1, 2).ok);
    CHECK(g.insertEdge(2, 3).ok);
    InsertResult overEdge = g.insertEdge(3, 4);  // needs a 4th vertex
    CHECK(overEdge.rejected);
    CHECK(!overEdge.ok);
    CHECK(overEdge.error == "node limit exceeded");
    CHECK(g.numNodes() == 3);
    CHECK(g.numEdges() == 2);

    // Existing 3 vertices, edge cap reached: any genuinely new edge is refused.
    InsertResult overCap = g.insertEdge(1, 3);
    CHECK(overCap.rejected);
    CHECK(overCap.error == "edge limit exceeded");
    CHECK(g.numEdges() == 2);

    // A duplicate edge is still idempotent at the edge cap.
    InsertResult dup = g.insertEdge(1, 2);
    CHECK(dup.ok && dup.duplicated);
    CHECK(g.numEdges() == 2);

    // Cycles are still reported (and unmutated) at the cap.
    InsertResult cyc = g.insertEdge(3, 1);
    CHECK(cyc.cycle && !cyc.ok);
    CHECK(g.numEdges() == 2);

    auto rep = g.verify();
    CHECK(rep.valid && rep.acyclic);
}

}  // namespace

int main() {
    testBasicForwardInsert();
    testReorderTriggersSearch();
    testSelfLoop();
    testDuplicateEdges();
    testCycleRejectedAndGraphUnchanged();
    testCyclePathEdgesExist();
    testIsolatedNodes();
    testReverseLongChain();
    testScaleLimit();

    fuzzAgainstNaive(/*seed=*/12345, /*n=*/60, /*trials=*/3000);
    fuzzAgainstNaive(/*seed=*/67890, /*n=*/120, /*trials=*/6000);
    fuzzAgainstNaive(/*seed=*/42, /*n=*/30, /*trials=*/2000);

    std::printf("\nchecks=%d failures=%d\n", g_checks, g_failures);
    return g_failures == 0 ? 0 : 1;
}
