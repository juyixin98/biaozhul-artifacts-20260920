// Self-contained C++ unit tests (no external test framework, matching the
// project's zero-dependency design). Each check prints PASS/FAIL; the
// process exits non-zero if anything failed.
#include "graph_builder.hpp"
#include "json.hpp"
#include "naive.hpp"
#include "scc.hpp"
#include "verify.hpp"

#include <algorithm>
#include <cstdio>
#include <functional>
#include <sstream>
#include <string>
#include <vector>

namespace {

int g_failures = 0;
int g_checks = 0;
std::string g_currentTest;

void beginTest(const std::string& name) {
    g_currentTest = name;
    std::printf("[ RUN      ] %s\n", name.c_str());
}

void check(bool cond, const std::string& what) {
    ++g_checks;
    if (!cond) {
        ++g_failures;
        std::printf("    FAIL: %s (in %s)\n", what.c_str(), g_currentTest.c_str());
    }
}

void endTest() {
    std::printf("[       OK ] %s\n", g_currentTest.c_str());
}

Graph graphFromEdges(int n, std::vector<std::pair<int, int>> edges) {
    json::Value req = json::Value::makeObject();
    req.set("vertices", json::Value::fromInt(n));
    json::Value earr = json::Value::makeArray();
    for (auto [u, v] : edges) {
        json::Value e = json::Value::makeObject();
        e.set("from", json::Value::fromInt(u));
        e.set("to", json::Value::fromInt(v));
        earr.push(std::move(e));
    }
    req.set("edges", std::move(earr));
    return buildGraph(req);
}

std::vector<int> compOf(const AnalysisResult& r, int n) {
    std::vector<int> out(static_cast<std::size_t>(n));
    for (int v = 0; v < n; ++v) out[static_cast<std::size_t>(v)] = r.compOf[static_cast<std::size_t>(v)];
    return out;
}

void testIsolatedVertices() {
    beginTest("isolated vertices each form singleton acyclic SCC");
    Graph g = graphFromEdges(4, {});
    AnalysisResult r = scc::analyze(g);
    check(r.components.size() == 4, "4 components");
    auto co = compOf(r, 4);
    check((co == std::vector<int>{0, 1, 2, 3}), "canonical ids 0..3");
    for (const auto& c : r.components)
        check(r.witnesses[static_cast<std::size_t>(c.id)].empty(), "no witness for isolated vertex");
    check(r.dagEdges.empty(), "no DAG edges");
    verify::CheckReport rep = verify::checkResult(g, r);
    check(rep.ok, "verifier ok");
    endTest();
}

void testSelfLoop() {
    beginTest("self-loop makes singleton cyclic with witness [v]");
    Graph g = graphFromEdges(3, {{1, 1}});
    AnalysisResult r = scc::analyze(g);
    check(r.components.size() == 3, "3 components");
    int c1 = r.compOf[1];
    check(r.witnesses[static_cast<std::size_t>(c1)] == std::vector<int>{1}, "witness [1]");
    check(r.witnesses[static_cast<std::size_t>(r.compOf[0])].empty(), "vertex 0 acyclic");
    check(r.witnesses[static_cast<std::size_t>(r.compOf[2])].empty(), "vertex 2 acyclic");
    check(r.dagEdges.empty(), "self-loop stays inside component, no DAG edge");
    check(verify::checkResult(g, r).ok, "verifier ok");
    endTest();
}

void testMultipleEdges() {
    beginTest("parallel edges aggregate; cross-SCC multiplicity preserved");
    // 0 -> 1 three times, no return: two SCCs, one DAG edge of multiplicity 3.
    Graph g = graphFromEdges(2, {{0, 1}, {0, 1}, {0, 1}});
    AnalysisResult r = scc::analyze(g);
    check(r.components.size() == 2, "2 components");
    check(g.uniqueEdges.size() == 1, "exactly one unique edge");
    check(g.uniqueEdges[0].multiplicity == 3, "multiplicity 3");
    check(r.dagEdges.size() == 1, "one DAG edge");
    check(r.dagEdges[0].multiplicity == 3, "DAG edge multiplicity 3");
    check(r.dagEdges[0].fromComponent == r.compOf[0], "from comp of 0");
    check(r.dagEdges[0].toComponent == r.compOf[1], "to comp of 1");
    check(verify::checkResult(g, r).ok, "verifier ok");
    endTest();
}

void testClassicDiamondWithCycle() {
    beginTest("mixed graph: one 3-cycle SCC plus source and sink");
    // 0 -> 1 <-> 2 <-> 3 -> 4 ; edges: cycle among 1,2,3, source 0, sink 4
    Graph g = graphFromEdges(5, {{0, 1}, {1, 2}, {2, 3}, {3, 1}, {3, 4}, {2, 1}});
    AnalysisResult r = scc::analyze(g);
    check(r.components.size() == 3, "3 components");
    check(r.compOf[1] == r.compOf[2], "1~2");
    check(r.compOf[2] == r.compOf[3], "2~3");
    check(r.compOf[0] != r.compOf[1], "0 separate");
    check(r.compOf[4] != r.compOf[1], "4 separate");
    // canonical: component with min 0 -> id 0, with min 1 -> id 1, with min 4 -> id 2
    check((compOf(r, 5) == std::vector<int>{0, 1, 1, 1, 2}), "canonical numbering");
    // DAG edges: 0->1 and 1->2
    check(r.dagEdges.size() == 2, "two DAG edges after dedup");
    check(r.dagEdges[0].fromComponent == 0 && r.dagEdges[0].toComponent == 1, "edge 0->1");
    check(r.dagEdges[1].fromComponent == 1 && r.dagEdges[1].toComponent == 2, "edge 1->2");
    // witness for the 3-cycle
    const auto& w = r.witnesses[1];
    check(!w.empty() && w.front() == 1, "witness starts at representative 1");
    check(verify::checkResult(g, r).ok, "verifier ok");
    endTest();
}

void testTwoCyclesWithParallelCrossEdges() {
    beginTest("two cyclic SCCs, bidirectional-but-not-really parallel cross edges");
    // SCC A: 0<->1, SCC B: 2<->3; cross edges 1->2 twice, 0->2 once (all A->B)
    Graph g = graphFromEdges(4, {{0, 1}, {1, 0}, {2, 3}, {3, 2}, {1, 2}, {1, 2}, {0, 2}});
    AnalysisResult r = scc::analyze(g);
    check(r.components.size() == 2, "2 components");
    check(r.dagEdges.size() == 1, "single deduped cross-SCC edge");
    check(r.dagEdges[0].multiplicity == 3, "cross multiplicity 3");
    check(!r.witnesses[static_cast<std::size_t>(r.compOf[0])].empty(), "A has witness");
    check(!r.witnesses[static_cast<std::size_t>(r.compOf[2])].empty(), "B has witness");
    check(verify::checkResult(g, r).ok, "verifier ok");
    endTest();
}

void testNaiveAgreement() {
    beginTest("Kosaraju partition agrees with naive mutual-reachability partition");
    // Hand graph with a self-loop, parallel edges, isolated node and a 2-cycle.
    Graph g = graphFromEdges(6, {
        {0, 1}, {1, 0}, {1, 0}, // 2-cycle with a duplicated edge
        {2, 2},                 // self-loop
        {3, 4}, {4, 5}, {3, 5}, // acyclic chain 3->4->5 plus chord
        // vertex index? all 0..5 present
    });
    AnalysisResult r = scc::analyze(g);
    std::vector<int> naive = naive::sccByReachability(g);
    bool same = true;
    for (int v = 0; v < g.n; ++v)
        if (naive[static_cast<std::size_t>(v)] != r.compOf[static_cast<std::size_t>(v)]) same = false;
    check(same, "partitions identical");

    // Reachability matrix spot checks.
    std::vector<char> reach = naive::reachabilityMatrix(g);
    auto R = [&](int u, int v) {
        return reach[static_cast<std::size_t>(u) * static_cast<std::size_t>(g.n) +
                     static_cast<std::size_t>(v)] != 0;
    };
    check(R(0, 1) && R(1, 0), "0 and 1 mutually reachable");
    check(R(3, 5) && !R(5, 3), "3 reaches 5 one way");
    check(R(4, 5) && !R(4, 3), "4 reaches 5 but not 3");
    check(!R(2, 3) && R(2, 2), "self-looped 2 is isolated from others");
    check(verify::checkResult(g, r).ok, "verifier ok");
    endTest();
}

void testDeterminismUnderEdgePermutation() {
    beginTest("output is invariant under edge-list permutation");
    std::vector<std::pair<int, int>> edges = {{0, 1}, {1, 2}, {2, 0}, {3, 1}, {1, 3}, {4, 4}};
    Graph g1 = graphFromEdges(5, edges);
    std::reverse(edges.begin(), edges.end());
    Graph g2 = graphFromEdges(5, edges);
    AnalysisResult a = scc::analyze(g1);
    AnalysisResult b = scc::analyze(g2);
    check(compOf(a, 5) == compOf(b, 5), "same compOf");
    check(a.dagEdges.size() == b.dagEdges.size(), "same DAG edge count");
    for (std::size_t i = 0; i < a.dagEdges.size(); ++i) {
        check(a.dagEdges[i].fromComponent == b.dagEdges[i].fromComponent &&
                  a.dagEdges[i].toComponent == b.dagEdges[i].toComponent &&
                  a.dagEdges[i].multiplicity == b.dagEdges[i].multiplicity,
              "identical DAG edge at index " + std::to_string(i));
    }
    endTest();
}

void testLabeledInput() {
    beginTest("string labels and edge multiplicities parse correctly");
    json::Value req = json::Value::makeObject();
    json::Value verts = json::Value::makeArray();
    for (const char* l : {"A", "B", "C"}) verts.push(json::Value::fromString(l));
    req.set("vertices", std::move(verts));
    json::Value earr = json::Value::makeArray();
    for (auto [f, t] : {std::pair<std::string, std::string>{"A", "B"}, {"B", "A"}, {"A", "B"}}) {
        json::Value e = json::Value::makeObject();
        e.set("from", json::Value::fromString(f));
        e.set("to", json::Value::fromString(t));
        earr.push(std::move(e));
    }
    req.set("edges", std::move(earr));
    Graph g = buildGraph(req);
    check(g.labels.size() == 3, "3 labels");
    AnalysisResult r = scc::analyze(g);
    check(r.compOf[0] == r.compOf[1], "A~B");
    check(r.compOf[2] != r.compOf[0], "C separate");
    check(verify::checkResult(g, r).ok, "verifier ok");
    endTest();
}

void testRejectsBadInput() {
    beginTest("malformed requests are rejected");
    auto mustFail = [&](const std::string& label, json::Value req) {
        bool threw = false;
        try { buildGraph(req); } catch (const std::exception&) { threw = true; }
        check(threw, "rejected: " + label);
    };
    mustFail("missing edges", json::Value::makeObject());
    {
        json::Value req = json::Value::makeObject();
        req.set("edges", json::Value::fromInt(3));
        mustFail("edges not array", req);
    }
    {
        json::Value req = json::Value::makeObject();
        req.set("vertices", json::Value::fromInt(2));
        json::Value earr = json::Value::makeArray();
        json::Value e = json::Value::makeObject();
        e.set("from", json::Value::fromInt(0));
        e.set("to", json::Value::fromInt(5));
        earr.push(std::move(e));
        req.set("edges", std::move(earr));
        mustFail("endpoint out of range", req);
    }
    {
        json::Value req = json::Value::makeObject();
        json::Value verts = json::Value::makeArray();
        verts.push(json::Value::fromString("A"));
        verts.push(json::Value::fromString("A"));
        req.set("vertices", std::move(verts));
        req.set("edges", json::Value::makeArray());
        mustFail("duplicate labels", req);
    }
    endTest();
}

void testVerifierCatchesTampering() {
    beginTest("independent verifier catches deliberately corrupted results");

    // Chain 0 -> 1 -> 2: three singleton SCCs, no cycles.
    Graph g = graphFromEdges(3, {{0, 1}, {1, 2}});
    AnalysisResult good = scc::analyze(g);

    // 1. Forging a back-edge in the condensation must be reported as a cycle.
    {
        AnalysisResult bad = good;
        bad.dagEdges.push_back({2, 0, 1});
        verify::CheckReport rep = verify::checkResult(g, bad);
        check(!rep.ok, "Kahn catches condensation cycle");
        bool mentionsCycle = std::any_of(rep.failures.begin(), rep.failures.end(),
                                         [](const std::string& m) { return m.find("cycle") != std::string::npos; });
        check(mentionsCycle, "failure message mentions cycle");
    }

    // 2. Merging two non-mutually-reachable vertices violates maximality / connectivity.
    {
        AnalysisResult bad = good;
        // Rebuild components: put vertices 0 and 1 together, keep 2 separate.
        bad.components.clear();
        bad.witnesses.clear();
        bad.compOf = {0, 0, 1};
        bad.components.push_back({0, {0, 1}, 0});
        bad.components.push_back({1, {2}, 2});
        bad.dagEdges.clear();
        bad.witnesses = {{}, {}};
        verify::CheckReport rep = verify::checkResult(g, bad);
        check(!rep.ok, "split/merge violation detected");
    }

    // 3. A witness using a vertex outside the component / a nonexistent edge must be rejected.
    {
        Graph g2 = graphFromEdges(3, {{0, 1}, {1, 0}}); // vertex 2 isolated
        AnalysisResult r2 = scc::analyze(g2);
        r2.witnesses[static_cast<std::size_t>(r2.compOf[0])] = {0, 1}; // valid first
        check(verify::checkResult(g2, r2).ok, "valid witness accepted");
        r2.witnesses[static_cast<std::size_t>(r2.compOf[0])] = {0, 1, 2}; // vertex 2 not in component
        check(!verify::checkResult(g2, r2).ok, "witness leaving component rejected");
    }

    // 4. A duplicated condensation edge must be rejected.
    {
        AnalysisResult bad = good;
        bad.dagEdges.push_back(bad.dagEdges.front());
        check(!verify::checkResult(g, bad).ok, "duplicate DAG edge rejected");
    }
    endTest();
}

} // namespace

int main() {
    std::vector<std::function<void()>> tests = {
        testIsolatedVertices,
        testSelfLoop,
        testMultipleEdges,
        testClassicDiamondWithCycle,
        testTwoCyclesWithParallelCrossEdges,
        testNaiveAgreement,
        testDeterminismUnderEdgePermutation,
        testLabeledInput,
        testRejectsBadInput,
        testVerifierCatchesTampering,
    };
    for (auto& t : tests) t();
    std::printf("\n%d checks, %d failures\n", g_checks, g_failures);
    return g_failures == 0 ? 0 : 1;
}
