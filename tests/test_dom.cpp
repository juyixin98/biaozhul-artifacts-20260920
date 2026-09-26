// Unit / differential tests for the dominance solvers.
//
// Strategy:
//   * Hand-computed expectations for structural cases required by the spec
//     (back edges, multiple exits, unreachable cycles, self loops,
//     irreducible loops, unreachable nodes feeding reachable merges).
//   * Randomized differential testing over thousands of small graphs:
//     Lengauer-Tarjan vs naive set iteration vs exhaustive simple-path
//     enumeration must agree on idom / dom sets / frontiers.
//   * A bounded scale run with timing evidence.
#include "../src/graph.hpp"
#include "../src/dominance.hpp"

#include <cstdio>
#include <cstdlib>
#include <functional>
#include <random>
#include <string>
#include <vector>
#include <chrono>
#include <algorithm>
#include <sstream>
#include <unordered_map>

namespace {

int g_failures = 0;
int g_checks = 0;

void check(bool cond, const std::string& msg, const char* file, int line) {
    ++g_checks;
    if (!cond) {
        ++g_failures;
        std::printf("FAIL %s:%d  %s\n", file, line, msg.c_str());
    }
}

#define CHECK(cond, msg) check((cond), (msg), __FILE__, __LINE__)

using Edges = std::vector<std::pair<std::string, std::string>>;

dom::Graph makeGraph(const std::vector<std::string>& nodes,
                     const Edges& edges,
                     const std::string& entry) {
    return dom::Graph::build(nodes, edges, entry);
}

// Expected structures are expressed directly in labels.
struct Expectations {
    // node -> idom label ("" for unreachable, "SELF" for itself)
    std::vector<std::pair<std::string, std::string>> idom;
    // node -> sorted frontier labels
    std::vector<std::pair<std::string, std::vector<std::string>>> frontier;
    std::vector<std::string> unreachable;
};

void verifyCase(const std::string& name,
                const std::vector<std::string>& nodes,
                const Edges& edges,
                const std::string& entry,
                const Expectations& exp,
                bool runReferences = true,
                long long pathCap = 200000) {
    dom::Graph g = makeGraph(nodes, edges, entry);
    dom::LTResult lt = dom::lengauerTarjan(g);

    std::unordered_map<std::string, int> id;
    for (int i = 0; i < g.n(); ++i) id.emplace(g.labels[i], i);

    for (const auto& [node, idomLabel] : exp.idom) {
        int v = id[node];
        int got = lt.idom[v];
        std::string gotLabel;
        if (got == -1) gotLabel = "";
        else if (got == v) gotLabel = "SELF";
        else gotLabel = g.labels[got];
        CHECK(gotLabel == idomLabel,
              name + ": idom(" + node + ")=" + gotLabel + " expected " + idomLabel);
    }
    for (const auto& [node, fLabels] : exp.frontier) {
        int v = id[node];
        std::vector<std::string> got;
        for (int x : lt.df[v]) got.push_back(g.labels[x]);
        std::vector<std::string> want = fLabels;
        std::sort(want.begin(), want.end());
        std::sort(got.begin(), got.end());
        CHECK(got == want,
              name + ": DF(" + node + ") mismatch, got " +
              [&]{ std::string s="["; for(auto& z:got){s+=z;s+=",";} s+="]"; return s; }());
    }
    std::vector<std::string> gotUnreach;
    for (int v = 0; v < g.n(); ++v) if (!lt.reachable[v]) gotUnreach.push_back(g.labels[v]);
    std::sort(gotUnreach.begin(), gotUnreach.end());
    std::vector<std::string> wantUnreach = exp.unreachable;
    std::sort(wantUnreach.begin(), wantUnreach.end());
    CHECK(gotUnreach == wantUnreach, name + ": unreachable set mismatch");

    if (runReferences) {
        dom::NaiveResult nv = dom::naiveIterative(g);
        CHECK(dom::sameIdom(lt.idom, nv.idom), name + ": LT vs naive idom disagreement");
        CHECK(dom::sameFrontiers(lt.df, nv.df), name + ": LT vs naive frontier disagreement");

        std::vector<std::vector<int>> ltSets(g.n());
        for (int v : lt.preorder) {
            for (int x = v;; x = lt.idom[x]) {
                ltSets[v].push_back(x);
                if (x == lt.idom[x]) break;
            }
            std::sort(ltSets[v].begin(), ltSets[v].end());
        }
        CHECK(dom::sameDomSets(ltSets, nv.domSets), name + ": idom-chain sets vs naive dom sets");

        try {
            dom::PathEnumResult pe = dom::pathEnumeration(g, pathCap);
            CHECK(dom::sameDomSets(nv.domSets, pe.domSets),
                  name + ": naive vs simple-path-enumeration dom sets");
            std::printf("  [%s] simple paths enumerated: %lld, naive fixpoint rounds: %d\n",
                        name.c_str(), pe.totalPaths, nv.iterations);
        } catch (const std::exception& e) {
            CHECK(false, name + ": path enumeration unexpectedly failed: " + e.what());
        }
    }
}

// ------------------------------------------------------------- hand cases

void testSingleNode() {
    verifyCase("single", {"a"}, {}, "a", {
        /*idom*/ {{"a", "SELF"}},
        /*frontier*/ {{"a", {}}},
        /*unreachable*/ {}
    });
}

void testChain() {
    Edges e = {{"a","b"},{"b","c"}};
    verifyCase("chain", {"a","b","c"}, e, "a", {
        {{"a","SELF"},{"b","a"},{"c","b"}},
        {{"a",{}},{"b",{}},{"c",{}}},
        {}
    });
}

void testDiamond() {
    Edges e = {{"a","b"},{"a","c"},{"b","d"},{"c","d"}};
    verifyCase("diamond", {"a","b","c","d"}, e, "a", {
        {{"a","SELF"},{"b","a"},{"c","a"},{"d","a"}},
        {{"a",{}},{"b",{"d"}},{"c",{"d"}},{"d",{}}},
        {}
    });
}

// Back edge c -> b creates a natural loop with header b.
void testBackEdge() {
    Edges e = {{"a","b"},{"b","c"},{"c","b"},{"b","d"}};
    verifyCase("back-edge", {"a","b","c","d"}, e, "a", {
        {{"a","SELF"},{"b","a"},{"c","b"},{"d","b"}},
        // DF(b) contains b itself because the back edge reaches the header;
        // DF(c) contains b (c branches back to a node c does not dominate).
        {{"a",{}},{"b",{"b"}},{"c",{"b"}},{"d",{}}},
        {}
    });
}

void testMultipleExits() {
    Edges e = {{"a","b"},{"a","c"},{"b","d"},{"c","d"},{"b","e"},{"c","f"}};
    verifyCase("multi-exit", {"a","b","c","d","e","f"}, e, "a", {
        {{"a","SELF"},{"b","a"},{"c","a"},{"d","a"},{"e","b"},{"f","c"}},
        {{"a",{}},{"b",{"d"}},{"c",{"d"}},{"d",{}},{"e",{}},{"f",{}}},
        {}
    });
}

// c<->d is an unreachable cycle; e feeds it but is itself unreachable.
void testUnreachableCycle() {
    Edges e = {{"a","b"},{"c","d"},{"d","c"},{"e","c"}};
    verifyCase("unreachable-cycle", {"a","b","c","d","e"}, e, "a", {
        {{"a","SELF"},{"b","a"},{"c",""},{"d",""},{"e",""}},
        {{"a",{}},{"b",{}},{"c",{}},{"d",{}},{"e",{}}},
        {"c","d","e"}
    });
}

// An unreachable node u with an edge into the reachable merge d must not
// influence dominators or frontiers (no entry path routes through u).
void testUnreachableFeedingReachable() {
    Edges e = {{"a","b"},{"a","c"},{"b","d"},{"c","d"},{"u","d"},{"u","b"}};
    verifyCase("unreachable-feeds-reachable", {"a","b","c","d","u"}, e, "a", {
        {{"a","SELF"},{"b","a"},{"c","a"},{"d","a"},{"u",""}},
        {{"a",{}},{"b",{"d"}},{"c",{"d"}},{"d",{}},{"u",{}}},
        {"u"}
    });
}

// Regression from differential fuzz: a back edge into the ENTRY node puts
// entry in its own dominance frontier under the formal Cytron definition.
void testBackEdgeIntoEntry() {
    Edges e = {{"a","b"},{"b","a"},{"a","c"}};
    verifyCase("back-edge-into-entry", {"a","b","c"}, e, "a", {
        {{"a","SELF"},{"b","a"},{"c","a"}},
        {{"a",{"a"}},{"b",{"a"}},{"c",{}}},
        {}
    });
}

void testSelfLoop() {
    Edges e = {{"a","a"},{"a","b"}};
    verifyCase("self-loop", {"a","b"}, e, "a", {
        {{"a","SELF"},{"b","a"}},
        // the self edge a->a puts a in its own frontier.
        {{"a",{"a"}},{"b",{}}},
        {}
    });
}

// Irreducible loop: headerless cycle b<->c entered from both branches.
void testIrreducible() {
    Edges e = {{"a","b"},{"a","c"},{"b","c"},{"c","b"},{"b","d"},{"c","d"}};
    verifyCase("irreducible", {"a","b","c","d"}, e, "a", {
        {{"a","SELF"},{"b","a"},{"c","a"},{"d","a"}},
        {{"a",{}},{"b",{"c","d"}},{"c",{"b","d"}},{"d",{}}},
        {}
    });
}

// Cooper-Harvey-Kennedy "A Simple, Fast Dominance Algorithm" example graph.
void testCHK() {
    Edges e = {{"R","A"},{"R","B"},{"A","C"},{"B","C"},{"C","D"},{"C","E"},
               {"D","F"},{"E","F"},{"F","G"}};
    verifyCase("chk-paper", {"R","A","B","C","D","E","F","G"}, e, "R", {
        {{"R","SELF"},{"A","R"},{"B","R"},{"C","R"},
         {"D","C"},{"E","C"},{"F","C"},{"G","F"}},
        {{"R",{}},{"A",{"C"}},{"B",{"C"}},{"C",{}},
         {"D",{"F"}},{"E",{"F"}},{"F",{}},{"G",{}}},
        {}
    });
}

void testValidationErrors() {
    bool threw = false;
    try { dom::Graph::build({}, {}, "x"); }
    catch (const std::exception&) { threw = true; }
    CHECK(threw, "empty graph must be rejected");

    threw = false;
    try { dom::Graph::build({"a","a"}, {}, "a"); }
    catch (const std::exception&) { threw = true; }
    CHECK(threw, "duplicate labels must be rejected");

    threw = false;
    try { dom::Graph::build({"a"}, {}, "z"); }
    catch (const std::exception&) { threw = true; }
    CHECK(threw, "unknown entry must be rejected");

    threw = false;
    try { dom::Graph::build({"a","b"}, {{"a","x"}}, "a"); }
    catch (const std::exception&) { threw = true; }
    CHECK(threw, "edge to unknown node must be rejected");

    dom::Limits tight;
    tight.maxNodes = 3;
    threw = false;
    try { dom::Graph::build({"a","b","c","d"}, {}, "a", tight); }
    catch (const std::exception&) { threw = true; }
    CHECK(threw, "node limit must be enforced");
}

void testQueries() {
    Edges e = {{"a","b"},{"a","c"},{"b","d"},{"c","d"},{"x","y"}};
    dom::Graph g = makeGraph({"a","b","c","d","x","y"}, e, "a");
    dom::LTResult lt = dom::lengauerTarjan(g);
    auto idx = [&](const std::string& s) {
        for (int i = 0; i < g.n(); ++i) if (g.labels[i] == s) return i;
        return -1;
    };
    CHECK(dom::dominates(lt, idx("a"), idx("d")), "a dominates d");
    CHECK(dom::dominates(lt, idx("b"), idx("b")), "reflexive dominance b dom b");
    CHECK(!dom::dominates(lt, idx("b"), idx("c")), "b does not dominate c");
    CHECK(!dom::dominates(lt, idx("b"), idx("d")), "b does not dominate d (alt path via c)");
    CHECK(dom::properlyDominates(lt, idx("a"), idx("b")), "a properly dominates b");
    CHECK(!dom::properlyDominates(lt, idx("a"), idx("a")), "a does not properly dominate itself");
    CHECK(!dom::dominates(lt, idx("a"), idx("x")), "reachable node cannot dominate unreachable node");
    CHECK(!dom::dominates(lt, idx("x"), idx("a")), "unreachable node cannot dominate reachable node");
    CHECK(dom::dominates(lt, idx("x"), idx("x")), "reflexive dominance even for unreachable node");
    CHECK(!dom::properlyDominates(lt, idx("x"), idx("y")), "no proper dominance among unreachable nodes");
}

// ------------------------------------------------------- differential fuzz

void fuzzOnce(std::mt19937& rng, int n, double edgeProb, bool allowUnreachable,
              int& ltVsNaive, int& pathCount) {
    std::vector<std::string> nodes;
    for (int i = 0; i < n; ++i) nodes.push_back("n" + std::to_string(i));

    // Guarantee node 0 can reach a prefix by chance structure; random edges do
    // the rest. Edges are mostly forward (index j > i) with some back edges
    // so loops occur but every node usually remains reachable when requested.
    Edges edges;
    std::uniform_real_distribution<double> u01(0.0, 1.0);
    for (int i = 0; i < n; ++i) {
        for (int j = i + 1; j < n; ++j) {
            if (u01(rng) < edgeProb) edges.emplace_back(nodes[i], nodes[j]);
        }
        // occasional back / cross edges (creates loops and irreducible regions)
        if (i >= 2 && u01(rng) < 0.15) {
            std::uniform_int_distribution<int> pick(0, i - 1);
            edges.emplace_back(nodes[i], nodes[pick(rng)]);
        }
    }
    if (!allowUnreachable) {
        // Force a spanning chain so every node is reachable from n0.
        for (int i = 0; i + 1 < n; ++i)
            edges.emplace_back(nodes[i], nodes[i + 1]);
    }
    // Occasional self loop.
    if (n > 2 && u01(rng) < 0.3) {
        std::uniform_int_distribution<int> pick(0, n - 1);
        edges.emplace_back(nodes[pick(rng)], nodes[pick(rng)]);
    }
    std::sort(edges.begin(), edges.end());
    edges.erase(std::unique(edges.begin(), edges.end()), edges.end());

    dom::Graph g = dom::Graph::build(nodes, edges, "n0");

    dom::LTResult lt = dom::lengauerTarjan(g);
    dom::NaiveResult nv = dom::naiveIterative(g);

    if (!dom::sameIdom(lt.idom, nv.idom)) {
        ++ltVsNaive;
        std::printf("FUZZ idom disagreement on %d-node graph, %zu edges\n", n, edges.size());
        for (auto& e2 : edges) std::printf("  %s->%s\n", e2.first.c_str(), e2.second.c_str());
        return;
    }
    if (!dom::sameFrontiers(lt.df, nv.df)) {
        ++ltVsNaive;
        std::printf("FUZZ frontier disagreement on %d-node graph, %zu edges\n", n, edges.size());
        if (std::getenv("DOM_DUMP")) {
            for (auto& e2 : edges) std::printf("  %s->%s\n", e2.first.c_str(), e2.second.c_str());
            for (int v = 0; v < g.n(); ++v) {
                if (lt.df[v] != nv.df[v]) {
                    std::printf("  DF mismatch for %s: LT=[", g.labels[v].c_str());
                    for (int x : lt.df[v]) std::printf("%s,", g.labels[x].c_str());
                    std::printf("] naive=[");
                    for (int x : nv.df[v]) std::printf("%s,", g.labels[x].c_str());
                    std::printf("]\n");
                }
            }
        }
        return;
    }

    // Only run exponential path enumeration when the graph is small/dense enough.
    try {
        dom::PathEnumResult pe = dom::pathEnumeration(g, 100000);
        ++pathCount;
        if (!dom::sameDomSets(nv.domSets, pe.domSets)) {
            ++ltVsNaive;
            std::printf("FUZZ path-enum dom-set disagreement\n");
        }
    } catch (const std::exception&) {
        // cap exceeded: skip exponential cross-check, set-based agreement still held
    }
}

void testFuzz() {
    std::mt19937 rng(0xC0FFEE);
    int disagreements = 0, enumerated = 0;
    for (int iter = 0; iter < 4000; ++iter) {
        std::uniform_int_distribution<int> nDist(1, 14);
        int n = nDist(rng);
        std::uniform_real_distribution<double> pDist(0.05, 0.45);
        bool allowUnreachable = (iter % 3 == 0);
        fuzzOnce(rng, n, pDist(rng), allowUnreachable, disagreements, enumerated);
    }
    std::printf("  [fuzz] 4000 random graphs; %d cross-checked by simple-path enumeration; "
                "disagreements: %d\n", enumerated, disagreements);
    CHECK(disagreements == 0, "differential fuzz found solver disagreement");
}

// ----------------------------------------------------------- path cap test

void testPathCap() {
    // Complete DAG on 8 nodes: 2^(n-2)=64 paths to the last node, total more.
    std::vector<std::string> nodes;
    Edges e;
    for (int i = 0; i < 10; ++i) nodes.push_back("v" + std::to_string(i));
    for (int i = 0; i < 10; ++i)
        for (int j = i + 1; j < 10; ++j)
            e.emplace_back(nodes[i], nodes[j]);
    dom::Graph g = makeGraph(nodes, e, "v0");
    bool threw = false;
    try { dom::pathEnumeration(g, 50); }
    catch (const std::exception& ex) { threw = true; std::printf("  [path-cap] %s\n", ex.what()); }
    CHECK(threw, "path enumeration must abort beyond the cap");

    dom::PathEnumResult pe = dom::pathEnumeration(g, 5000000);
    // In a complete DAG, only v0 dominates any node.
    for (int v = 1; v < g.n(); ++v)
        CHECK(pe.domSets[v] == std::vector<int>({0, v}),
              "complete DAG: only entry dominates v" + std::to_string(v));
}

// ----------------------------------------------------------------- scale

void testScale() {
    const int N = 4000;
    std::vector<std::string> nodes;
    Edges e;
    nodes.reserve(N);
    for (int i = 0; i < N; ++i) nodes.push_back("b" + std::to_string(i));
    // Chain backbone plus forward skip-edges within a window, plus occasional
    // back edges forming many small loops.
    for (int i = 0; i + 1 < N; ++i) {
        e.emplace_back(nodes[i], nodes[i + 1]);
        if (i + 3 < N) e.emplace_back(nodes[i], nodes[i + 3]);
        if (i + 7 < N && i % 5 == 0) e.emplace_back(nodes[i + 7], nodes[i + 1]);
    }
    dom::Limits lim; lim.maxNodes = 5000; lim.maxEdges = 50000;
    dom::Graph g = dom::Graph::build(nodes, e, "b0", lim);

    auto t0 = std::chrono::steady_clock::now();
    dom::LTResult lt = dom::lengauerTarjan(g);
    auto t1 = std::chrono::steady_clock::now();
    long ms = std::chrono::duration_cast<std::chrono::milliseconds>(t1 - t0).count();
    std::printf("  [scale] LT on %d nodes / %zu edges: %ld ms\n", N, e.size(), ms);

    CHECK(lt.reachableCount == N, "all chain nodes reachable");
    // Verify structural invariants that must hold for ANY correct idom tree.
    for (int v : lt.preorder) {
        if (v == g.entry) { CHECK(lt.idom[v] == v, "entry idom is itself"); continue; }
        CHECK(lt.idom[v] != -1, "reachable node has an idom");
        CHECK(lt.idom[v] != v, "non-entry is not its own idom");
    }
    int treeEdges = 0;
    for (int v = 0; v < g.n(); ++v)
        if (lt.reachable[v] && v != g.entry) ++treeEdges;
    CHECK(treeEdges == N - 1, "idom tree has N-1 edges");

    // Naive solver at reduced size for timing comparison evidence.
    const int M = 1200;
    std::vector<std::string> nodes2;
    Edges e2;
    for (int i = 0; i < M; ++i) nodes2.push_back("c" + std::to_string(i));
    for (int i = 0; i + 1 < M; ++i) {
        e2.emplace_back(nodes2[i], nodes2[i + 1]);
        if (i + 3 < M) e2.emplace_back(nodes2[i], nodes2[i + 3]);
    }
    dom::Graph g2 = dom::Graph::build(nodes2, e2, "c0");
    auto u0 = std::chrono::steady_clock::now();
    dom::NaiveResult nv2 = dom::naiveIterative(g2);
    auto u1 = std::chrono::steady_clock::now();
    long ms2 = std::chrono::duration_cast<std::chrono::milliseconds>(u1 - u0).count();
    dom::LTResult lt2 = dom::lengauerTarjan(g2);
    CHECK(dom::sameIdom(lt2.idom, nv2.idom), "scale cross-check idom");
    std::printf("  [scale] naive iterative on %d nodes: %ld ms (%d fixpoint rounds)\n",
                M, ms2, nv2.iterations);
}

} // namespace

int main() {
    testSingleNode();
    testChain();
    testDiamond();
    testBackEdge();
    testBackEdgeIntoEntry();
    testMultipleExits();
    testUnreachableCycle();
    testUnreachableFeedingReachable();
    testSelfLoop();
    testIrreducible();
    testCHK();
    testValidationErrors();
    testQueries();
    testPathCap();
    testFuzz();
    testScale();

    std::printf("\n%d checks, %d failures\n", g_checks, g_failures);
    return g_failures == 0 ? 0 : 1;
}
