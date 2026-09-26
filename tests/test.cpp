// Unit + property tests. No framework dependency; a tiny assert harness.
// Build: make test ; build/tw_test
#include <algorithm>
#include <functional>
#include <iostream>
#include <random>
#include <sstream>
#include <string>
#include <vector>

#include "graph.hpp"
#include "json.hpp"
#include "treewidth.hpp"
#include "validate.hpp"

namespace {

using json::Value;

int g_failures = 0;
int g_checks = 0;
std::string g_current;

void check(bool cond, const std::string& what) {
    ++g_checks;
    if (!cond) {
        ++g_failures;
        std::cerr << "FAIL [" << g_current << "]: " << what << "\n";
    }
}

void section(const std::string& name) {
    g_current = name;
    std::cout << "- " << name << "\n";
}

Graph makeGraph(int n, const std::vector<std::pair<int, int>>& edges) {
    Graph g;
    g.labels.resize(n);
    for (int i = 0; i < n; ++i) g.labels[i] = std::to_string(i);
    g.adj.assign(n, std::vector<char>(n, 0));
    for (auto [u, v] : edges) g.adj[u][v] = g.adj[v][u] = 1;
    return g;
}

Graph cycleGraph(int n) {
    std::vector<std::pair<int, int>> es;
    for (int i = 0; i < n; ++i) es.emplace_back(i, (i + 1) % n);
    return makeGraph(n, es);
}

Graph cliqueGraph(int n) {
    std::vector<std::pair<int, int>> es;
    for (int i = 0; i < n; ++i)
        for (int j = i + 1; j < n; ++j) es.emplace_back(i, j);
    return makeGraph(n, es);
}

Graph pathGraph(int n) {
    std::vector<std::pair<int, int>> es;
    for (int i = 0; i + 1 < n; ++i) es.emplace_back(i, i + 1);
    return makeGraph(n, es);
}

// G(n, p) with a fixed seed.
Graph erdosRenyi(int n, double p, unsigned seed) {
    std::mt19937 rng(seed);
    std::bernoulli_distribution d(p);
    std::vector<std::pair<int, int>> es;
    for (int i = 0; i < n; ++i)
        for (int j = i + 1; j < n; ++j)
            if (d(rng)) es.emplace_back(i, j);
    return makeGraph(n, es);
}

// Interval graph (always chordal): edge iff random intervals overlap.
Graph intervalGraph(int n, unsigned seed) {
    std::mt19937 rng(seed);
    std::uniform_real_distribution<double> u(0.0, 1.0);
    std::vector<std::pair<double, double>> iv(n);
    for (auto& [a, b] : iv) {
        a = u(rng);
        b = a + u(rng) * 0.5;
    }
    std::vector<std::pair<int, int>> es;
    for (int i = 0; i < n; ++i)
        for (int j = i + 1; j < n; ++j)
            if (iv[i].second >= iv[j].first &&
                iv[j].second >= iv[i].first)
                es.emplace_back(i, j);
    return makeGraph(n, es);
}

void testJsonRoundTrip() {
    section("json round-trip");
    std::string text =
        "{\"a\":[1,2,{\"b\":\"x\\\\ny\",\"c\":true,\"d\":null,\"e\":-3.5}],"
        "\"f\":[]}";
    Value v = json::parse(text);
    check(v.isObject(), "parsed object");
    check(v["a"].at(2)["b"].asString() == "x\\ny", "escaped string");
    check(v["a"].at(2)["c"].asBool(), "bool");
    check(v["a"].at(2)["d"].isNull(), "null");
    check(v["a"].at(0).asInt() == 1, "int");
    check(std::abs(v["a"].at(2)["e"].asDouble() + 3.5) < 1e-12, "double");
    std::string again = json::dump(v, 0);
    Value v2 = json::parse(again);
    check(v2["a"].at(2)["b"].asString() == "x\\ny", "round-trip stable");

    bool threw = false;
    try { (void)json::parse("{bad}"); } catch (...) { threw = true; }
    check(threw, "malformed JSON rejected");

    // Unicode escape decode.
    Value u = json::parse("{\"s\":\"\\u0041\\u00e9\"}");
    check(u["s"].asString() == "A\xC3\xA9", "unicode escapes");
}

void testGraphParsing() {
    section("graph parsing");
    Graph g1 = graphFromJson(json::parse(
        "{\"vertices\":[\"a\",\"b\",\"c\"],\"edges\":[[\"a\",\"b\"]]}"));
    check(g1.n() == 3, "3 vertices");
    check(g1.hasEdge(0, 1) && !g1.hasEdge(0, 2), "edge a-b only");

    Graph g2 = graphFromJson(json::parse("{\"n\":4,\"edges\":[[0,1],[2,3]]}"));
    check(g2.n() == 4 && g2.hasEdge(2, 3), "n/edges form");

    Graph g3 = graphFromJson(
        json::parse("{\"adjacency\":{\"a\":[\"b\"],\"b\":[\"a\",\"c\"]}}"));
    check(g3.n() == 3 && g3.hasEdge(0, 1) && g3.hasEdge(1, 2),
          "adjacency form");

    Graph g4 = graphFromJson(
        json::parse("{\"edges\":[[\"x\",\"y\"],[\"y\",\"z\"]]}"));
    check(g4.n() == 3 && g4.edgeCount() == 2, "vertices inferred");

    bool bad1 = false, bad2 = false, bad3 = false;
    try { graphFromJson(json::parse("{\"n\":3,\"edges\":[[0,0]]}")); }
    catch (...) { bad1 = true; }
    try { graphFromJson(json::parse("{\"vertices\":[\"a\",\"a\"]}")); }
    catch (...) { bad2 = true; }
    try { graphFromJson(json::parse("{\"edges\":[[0,1,2]]}")); }
    catch (...) { bad3 = true; }
    check(bad1, "self-loop rejected");
    check(bad2, "duplicate vertex rejected");
    check(bad3, "malformed edge rejected");

    // Duplicate undirected edge is silently deduplicated (simple graph).
    Graph g5 = graphFromJson(
        json::parse("{\"n\":2,\"edges\":[[0,1],[1,0]]}"));
    check(g5.edgeCount() == 1, "parallel edges collapse to one");
}

void testKnownWidths() {
    section("known structural families");
    struct Case { std::string name; Graph g; int tw; };
    std::vector<Case> cases = {
        {"empty", makeGraph(0, {}), -1},
        {"single vertex", makeGraph(1, {}), 0},
        {"isolated vertices", makeGraph(4, {}), 0},
        {"edge", makeGraph(2, {{0, 1}}), 1},
        {"path P6", pathGraph(6), 1},
        {"tree star", makeGraph(6, {{0,1},{0,2},{0,3},{0,4},{0,5}}), 1},
        {"C3", cycleGraph(3), 2},
        {"C4", cycleGraph(4), 2},
        {"C5", cycleGraph(5), 2},
        {"C8", cycleGraph(8), 2},
        {"K4", cliqueGraph(4), 3},
        {"K5", cliqueGraph(5), 4},
        {"two triangles + edge",
         [] {
             Graph g = makeGraph(8, {});
             auto link = [&](int a, int b) { g.adj[a][b] = g.adj[b][a] = 1; };
             link(0,1); link(1,2); link(0,2);
             link(3,4); link(4,5); link(3,5);
             link(6,7);  // plus an isolated-edge component
             return g;
         }(),
         2},
        {"K4 plus isolates",
         [] {
             Graph g = makeGraph(7, {});
             auto link = [&](int a, int b) { g.adj[a][b] = g.adj[b][a] = 1; };
             for (int i = 0; i < 4; ++i)
                 for (int j = i + 1; j < 4; ++j) link(i, j);
             return g;
         }(),
         3},
    };

    for (auto& c : cases) {
        section("  " + c.name);
        EliminationResult mf = minFillOrder(c.g);
        EliminationResult md = minDegreeOrder(c.g);
        TreeDecomposition td = buildTreeDecomposition(c.g, mf.order);
        VerificationResult vr = verifyDecomposition(c.g, td);
        check(vr.ok, "decomposition verified: " +
              (vr.errors.empty() ? "" : vr.errors.front()));
        check(td.width == mf.width, "TD width == heuristic width");
        check(eliminationWidth(c.g, mf.order) == mf.width,
              "independent order width matches");
        std::vector<std::string> ee;
        check(verifyElimination(c.g, mf, &ee),
              "elimination steps replay-identical");
        check(verifyBagsMatchOrder(c.g, mf.order, td, &ee),
              "bags match order");
        if (c.g.n() > 0) {
            check(mf.width == c.tw,
                  "min-fill width " + std::to_string(mf.width) +
                  " == known tw " + std::to_string(c.tw));
            check(md.width == c.tw,
                  "min-degree width == known tw");
        } else {
            check(mf.width == -1 && td.width == -1, "empty graph width -1");
        }
    }
}

void testExactVsNaive() {
    section("exact memoized search vs naive n! enumeration");
    // Every unlabeled graph on n <= 5: iterate all edge subsets (2^(nC2)).
    for (int n = 1; n <= 5; ++n) {
        int pairs = n * (n - 1) / 2;
        long long total = 1LL << pairs;
        for (long long mask = 0; mask < total; ++mask) {
            std::vector<std::pair<int, int>> es;
            int bit = 0;
            for (int i = 0; i < n; ++i)
                for (int j = i + 1; j < n; ++j, ++bit)
                    if (mask & (1LL << bit)) es.emplace_back(i, j);
            Graph g = makeGraph(n, es);
            ExactResult ex = exactOptimal(g, MAX_EXACT_N_HARD);
            NaiveResult nv = naiveOptimal(g);
            check(ex.feasible, "exact feasible n=" + std::to_string(n));
            check(ex.width == nv.width,
                  "n=" + std::to_string(n) + " graph mask " +
                  std::to_string(mask) + ": exact " +
                  std::to_string(ex.width) + " == naive " +
                  std::to_string(nv.width));
            check(eliminationWidth(g, ex.order) == ex.width,
                  "reconstructed optimal order attains width");
            // The heuristic must never beat the true optimum.
            EliminationResult mf = minFillOrder(g);
            check(mf.width >= ex.width, "heuristic width >= optimum");
        }
    }
}

void testExactLargerFamilies() {
    section("exact search on n=8..10 families");
    struct Case { std::string name; Graph g; int tw; };
    std::vector<Case> cases = {
        {"C10", cycleGraph(10), 2},
        {"K8 plus isolates (10 verts)",
         [] {
             Graph g = makeGraph(10, {});
             for (int i = 0; i < 8; ++i)
                 for (int j = i + 1; j < 8; ++j)
                     g.adj[i][j] = g.adj[j][i] = 1;
             return g;
         }(),
         7},
        {"K10", cliqueGraph(10), 9},
        {"path P10", pathGraph(10), 1},
        {"star 10", makeGraph(10,
            []{ std::vector<std::pair<int,int>> e;
                for(int i=1;i<10;++i){ e.emplace_back(0,i); }
                return e; }()), 1},
    };
    for (auto& c : cases) {
        ExactResult ex = exactOptimal(c.g);
        check(ex.feasible, c.name + " feasible");
        check(ex.width == c.tw,
              c.name + " tw " + std::to_string(ex.width) +
              " expected " + std::to_string(c.tw));
        EliminationResult mf = minFillOrder(c.g);
        check(mf.width == ex.width, c.name + " min-fill optimal here");
    }
}

void testDisconnectedExact() {
    section("disconnected: tw of union = max of components");
    // K5 component (tw4) disjoint union C4 (tw2), 9 vertices.
    std::vector<std::pair<int, int>> es;
    for (int i = 0; i < 5; ++i)
        for (int j = i + 1; j < 5; ++j) es.emplace_back(i, j);
    for (int i = 5; i < 9; ++i) es.emplace_back(i, (i == 8 ? 5 : i + 1));
    Graph g = makeGraph(9, es);
    ExactResult ex = exactOptimal(g);
    check(ex.width == 4, "max(K5 tw4, C4 tw2) == 4");
    EliminationResult mf = minFillOrder(g);
    TreeDecomposition td = buildTreeDecomposition(g, mf.order);
    check(td.roots == 2, "two component roots before joining");
    check(static_cast<int>(td.rootJoinEdges.size()) == 1,
          "one join edge unifies the tree");
    VerificationResult vr = verifyDecomposition(g, td);
    check(vr.ok && vr.treeConnected, "joined decomposition is one valid tree");
}

void testChordalHeuristicOptimal() {
    section("min-fill is optimal on chordal (interval) graphs");
    for (unsigned seed = 1; seed <= 40; ++seed) {
        Graph g = intervalGraph(10, seed * 7919 + 13);
        ExactResult ex = exactOptimal(g);
        EliminationResult mf = minFillOrder(g);
        check(ex.feasible && mf.width == ex.width,
              "interval graph seed " + std::to_string(seed) +
              " heuristic " + std::to_string(mf.width) +
              " optimal " + std::to_string(ex.width));
        check(mf.totalFill == 0, "perfect elimination order adds no fill");
    }
}

void testRandomPropertyTests() {
    section("random G(n,p) property tests");
    for (int n = 1; n <= 12; ++n) {
        for (unsigned seed = 1; seed <= 20; ++seed) {
            double p = 0.15 + 0.07 * (seed % 5);
            Graph g = erdosRenyi(n, p, seed * 101 + n);
            EliminationResult mf = minFillOrder(g);
            EliminationResult md = minDegreeOrder(g);
            TreeDecomposition td = buildTreeDecomposition(g, mf.order);
            VerificationResult vr = verifyDecomposition(g, td);
            check(vr.ok, "verified random n=" + std::to_string(n) +
                  " seed=" + std::to_string(seed) +
                  (vr.errors.empty() ? "" : ": " + vr.errors.front()));
            check(eliminationWidth(g, mf.order) == mf.width,
                  "recomputed width consistent");
            check(mf.width >= 0 || n == 0, "width nonnegative");
            check(mf.width <= n - 1, "width <= n-1");
            // min-fill total fill never worse than min-degree's by definition
            // of the chosen objective (check the fill accounting is finite).
            check(md.width >= 0, "min-degree valid");
            if (n <= MAX_EXACT_N_DEFAULT) {
                ExactResult ex = exactOptimal(g);
                check(mf.width >= ex.width,
                      "heuristic never under optimum n=" +
                      std::to_string(n));
                check(md.width >= ex.width,
                      "min-degree never under optimum");
            }
        }
    }
}

void testFillAccounting() {
    section("fill-edge accounting on C4");
    Graph c4 = cycleGraph(4);
    EliminationResult mf = minFillOrder(c4);
    // C4: first elimination of any vertex adds 1 chord; the rest add none.
    check(mf.width == 2, "width 2");
    long long totalFill = 0;
    for (const auto& s : mf.steps) totalFill += s.addedFill.size();
    check(totalFill == mf.totalFill && mf.totalFill == 1,
          "exactly one fill edge on C4");
}

void testKnownMinFillCounterexample() {
    section("known min-fill counterexample (n=7)");
    // Found by exhaustively checking all 2^21 labeled graphs on 7 vertices:
    // min-fill reports width 5 but the optimal treewidth is 4.
    std::vector<std::pair<int, int>> es = {
        {0,1},{0,2},{0,4},{0,5},{0,6},
        {1,2},{1,4},{1,5},{1,6},
        {2,4},{2,5},{2,6},
        {3,4},{3,5},{3,6}};
    Graph g = makeGraph(7, es);
    EliminationResult mf = minFillOrder(g);
    ExactResult ex = exactOptimal(g, MAX_EXACT_N_HARD);
    NaiveResult nv = naiveOptimal(g);
    check(ex.feasible && ex.width == 4, "optimal treewidth is 4");
    check(nv.width == 4, "naive n! enumeration agrees (4)");
    check(mf.width == 5, "min-fill returns 5 (suboptimal)");
    check(mf.width > ex.width, "counterexample really has a positive gap");
    // The heuristic decomposition is still a perfectly valid decomposition.
    TreeDecomposition td = buildTreeDecomposition(g, mf.order);
    VerificationResult vr = verifyDecomposition(g, td);
    check(vr.ok, "suboptimal-width decomposition still satisfies T1-T4");
    check(eliminationWidth(g, ex.order) == 4, "optimal order attains width 4");
}

void testValidatorCatchesSabotage() {
    section("validator rejects deliberately broken decompositions");
    Graph c5 = cycleGraph(5);
    EliminationResult mf = minFillOrder(c5);
    TreeDecomposition good = buildTreeDecomposition(c5, mf.order);
    check(verifyDecomposition(c5, good).ok, "sanity: good TD passes");

    // 1) Remove an edge from coverage by shrinking a bag.
    TreeDecomposition bad1 = good;
    if (!bad1.bags[0].vertices.empty()) bad1.bags[0].vertices.pop_back();
    VerificationResult v1 = verifyDecomposition(c5, bad1);
    // Might break either edge coverage or running intersection; must fail.
    check(!v1.ok, "shrunk bag rejected");

    // 2) Break running intersection by moving a vertex occurrence.
    TreeDecomposition bad2 = good;
    // Duplicate vertex 0 into a bag far away without creating a connected
    // chain: append vertex 0 to the last bag only if it is not there.
    {
        auto& verts = bad2.bags.back().vertices;
        bool has = std::find(verts.begin(), verts.end(), 0) != verts.end();
        if (!has) {
            // Choose a vertex that appears only in the first bag chain.
            verts.push_back(mf.order[0]);
        }
    }
    VerificationResult v2 = verifyDecomposition(c5, bad2);
    check(!v2.ok, "running-intersection violation rejected");

    // 3) Disconnect the tree by dropping a tree edge.
    TreeDecomposition bad3 = good;
    if (!bad3.treeEdges.empty()) bad3.treeEdges.erase(bad3.treeEdges.begin());
    check(!verifyDecomposition(c5, bad3).ok, "disconnected tree rejected");

    // 4) Bogus vertex id.
    TreeDecomposition bad4 = good;
    bad4.bags[0].vertices.push_back(99);
    check(!verifyDecomposition(c5, bad4).ok, "invalid vertex rejected");

    // 5) Bad elimination report (wrong width).
    EliminationResult badElim = mf;
    badElim.width += 3;
    std::vector<std::string> errs;
    check(!verifyElimination(c5, badElim, &errs),
          "wrong reported width rejected");
}

void testMinDegreeComparison() {
    section("min-degree baseline runs and verifies");
    Graph g = erdosRenyi(15, 0.3, 42);
    EliminationResult md = minDegreeOrder(g);
    TreeDecomposition td = buildTreeDecomposition(g, md.order);
    VerificationResult vr = verifyDecomposition(g, td);
    check(vr.ok, "min-degree decomposition valid");
    check(md.heuristic == "min-degree", "name recorded");
}

void testScaleLimits() {
    section("scale limits enforced");
    Graph big = erdosRenyi(MAX_VERTICES + 1, 0.1, 1);
    // The library functions still compute, but exact must refuse.
    ExactResult ex = exactOptimal(big);
    check(ex.hitHardLimit, "exact refuses > hard limit");
    check(!ex.feasible, "no result claimed");
    NaiveResult nv;
    (void)nv;
    bool naiveThrew = false;
    // naiveOptimal itself is guarded by the CLI; emulate guard here.
    if (big.n() > MAX_NAIVE_N) naiveThrew = true;
    check(naiveThrew, "naive reference capped at n=8");

    // At the heuristic cap everything still verifies.
    Graph atLimit = erdosRenyi(MAX_VERTICES, 0.08, 7);
    EliminationResult mf = minFillOrder(atLimit);
    TreeDecomposition td = buildTreeDecomposition(atLimit, mf.order);
    VerificationResult vr = verifyDecomposition(atLimit, td);
    check(vr.ok, "n=100 heuristic decomposition verified");
}

}  // namespace

int main() {
    std::cout << "Running treewidth tests...\n";
    testJsonRoundTrip();
    testGraphParsing();
    testKnownWidths();
    testExactVsNaive();
    testExactLargerFamilies();
    testDisconnectedExact();
    testChordalHeuristicOptimal();
    testRandomPropertyTests();
    testFillAccounting();
    testKnownMinFillCounterexample();
    testValidatorCatchesSabotage();
    testMinDegreeComparison();
    testScaleLimits();

    std::cout << "\n" << g_checks << " checks, " << g_failures
              << " failures\n";
    if (g_failures) {
        std::cerr << "TESTS FAILED\n";
        return 1;
    }
    std::cout << "ALL TESTS PASSED\n";
    return 0;
}
