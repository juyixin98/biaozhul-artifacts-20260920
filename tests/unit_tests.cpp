// SPDX-License-Identifier: MIT
// Unit tests for BigInt, Graph and the JSON API layer.
// Minimal embedded harness: no external test framework required.
#include <functional>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

#include "../src/api.h"
#include "../src/bigint.h"
#include "../src/graph.h"
#include "../src/json.h"

using namespace dagpaths;

namespace {

int g_failures = 0;
int g_checks = 0;

void check(bool condition, const std::string& name) {
    ++g_checks;
    if (!condition) {
        ++g_failures;
        std::cerr << "FAIL: " << name << '\n';
    }
}

void checkThrows(const std::function<void()>& fn, const std::string& name) {
    bool threw = false;
    try {
        fn();
    } catch (const std::exception&) {
        threw = true;
    }
    check(threw, name);
}

Graph linearChain(int n) {
    std::vector<Edge> edges;
    for (int i = 0; i + 1 < n; ++i) edges.emplace_back(i, i + 1);
    return Graph::build(n, edges);
}

// Layered DAG: node i connects to every node j > i.
// Paths 0 -> n-1 number 2^(n-2) for n >= 2.
Graph completeForward(int n) {
    std::vector<Edge> edges;
    for (int i = 0; i < n; ++i)
        for (int j = i + 1; j < n; ++j)
            edges.emplace_back(i, j);
    return Graph::build(n, edges);
}

void testBigInt() {
    check(BigInt(0).str() == "0", "BigInt zero prints 0");
    check(BigInt(123456789012345678ULL).str() == "123456789012345678",
          "BigInt uint64 value");
    check(BigInt::parseDecimal("999999999999999999999999999999").str()
              == "999999999999999999999999999999",
          "BigInt parse 30 nines");
    check(BigInt::parseDecimal("1000000000").str() == "1000000000",
          "BigInt parse single-limb boundary");

    BigInt a = BigInt::parseDecimal("999999999999999999999");
    a += BigInt(2);
    check(a.str() == "1000000000000000000001", "BigInt addition carry across limbs");

    BigInt b = BigInt::parseDecimal("1000000000000000000000");
    b -= BigInt(1);
    check(b.str() == "999999999999999999999", "BigInt subtraction borrow across limbs");
    checkThrows([&] { BigInt x(1); x -= BigInt(2); },
                "BigInt subtraction underflow throws");

    check(BigInt(9) < BigInt(10), "BigInt compare different widths");
    check(!(BigInt(10) < BigInt(9)), "BigInt compare reverse");
    check(BigInt(10) <= BigInt(10), "BigInt compare equal");
}

void testGraphValidation() {
    checkThrows([] { Graph::build(0, {}); }, "node count 0 rejected");
    checkThrows([] { Graph::build(3, {{0, 3}}); }, "out-of-range edge rejected");
    checkThrows([] { Graph::build(3, {{1, 1}}); }, "self-loop rejected");
    checkThrows([] { Graph::build(3, {{0, 1}, {1, 2}, {2, 0}}); },
                "directed cycle rejected");
    checkThrows([] { Graph::build(3, {{0, 1}, {0, 1}}); },
                "duplicate edge rejected");
}

void testCounts() {
    Graph diamond = Graph::build(4, {{0, 1}, {0, 2}, {1, 3}, {2, 3}});
    check(diamond.countPaths(0, 3).str() == "2", "diamond has 2 paths");
    check(diamond.countPaths(0, 0).str() == "1", "s == t counts empty path");
    check(diamond.countPaths(3, 0).str() == "0", "backwards pair unreachable");
    check(diamond.countPaths(1, 2).str() == "0", "siblings unreachable");

    Graph chain = linearChain(5);
    check(chain.countPaths(0, 4).str() == "1", "chain has 1 path");
    check(chain.countPaths(0, 2).str() == "1", "chain subpath has 1 path");

    // Disconnected components: multiple sources and sinks.
    Graph parts = Graph::build(6, {{0, 1}, {1, 2}, {3, 4}, {4, 5}});
    check(parts.countPaths(0, 2).str() == "1", "component A reachable");
    check(parts.countPaths(3, 5).str() == "1", "component B reachable");
    check(parts.countPaths(0, 5).str() == "0", "cross-component unreachable");
    check(parts.countPaths(2, 3).str() == "0", "sink to other source unreachable");

    // 2^(n-2) paths in the complete-forward graph.
    Graph dense = completeForward(10);
    check(dense.countPaths(0, 9).str() == "256",
          "complete-forward 10 nodes has 2^8=256 paths");

    // Larger than uint64: complete-forward graph with 70 nodes -> 2^68 paths.
    Graph huge = completeForward(70);
    BigInt expected = BigInt(1);
    for (int i = 0; i < 68; ++i) expected += expected;
    check(huge.countPaths(0, 69) == expected,
          "complete-forward 70 nodes has exact 2^68 paths (beyond uint64)");
}

void testEnumerationAndOrder() {
    Graph diamond = Graph::build(4, {{0, 1}, {0, 2}, {1, 3}, {2, 3}});
    auto paths = diamond.enumeratePaths(0, 3);
    check(paths.size() == 2, "diamond enumeration count");
    check((paths[0] == std::vector<int>{0, 1, 3}), "diamond first path lex");
    check((paths[1] == std::vector<int>{0, 2, 3}), "diamond second path lex");

    Graph g = Graph::build(5, {
        {0, 1}, {0, 2}, {1, 3}, {1, 4}, {2, 4}, {3, 4}
    });
    paths = g.enumeratePaths(0, 4);
    const std::vector<std::vector<int>> expectedPaths = {
        {0, 1, 3, 4}, {0, 1, 4}, {0, 2, 4}
    };
    check(paths == expectedPaths, "enumeration is lexicographic by node id");

    // Unreachable pair enumerates nothing.
    check(g.enumeratePaths(3, 0).empty(), "unreachable enumerates zero paths");
    check(g.enumeratePaths(4, 4) == std::vector<std::vector<int>>{{4}},
          "s == t enumerates the single-node path");
}

void testKthAndRank() {
    Graph g = Graph::build(5, {
        {0, 1}, {0, 2}, {1, 3}, {1, 4}, {2, 4}, {3, 4}
    });
    const BigInt total = g.countPaths(0, 4);
    auto all = g.enumeratePaths(0, 4);
    check(total.str() == std::to_string(all.size()), "count matches enumeration");

    for (size_t i = 0; i < all.size(); ++i) {
        const BigInt k = BigInt(static_cast<uint64_t>(i + 1));
        const std::vector<int> got = g.kthPath(0, 4, k);
        check(got == all[i], "kthPath matches enumerated path at rank " + std::to_string(i + 1));
        check(g.rankOfPath(got) == k, "rankOfPath inverts kthPath");
    }

    checkThrows([&] { g.kthPath(0, 4, BigInt(4)); }, "k past end throws");
    checkThrows([&] { g.kthPath(0, 4, BigInt(0)); }, "k = 0 throws");
    checkThrows([&] { g.kthPath(2, 1, BigInt(1)); },
                "kth on unreachable pair throws");

    checkThrows([&] { g.rankOfPath({0, 4}); }, "rank rejects non-path (missing edge)");
    checkThrows([&] { g.rankOfPath({0, 9}); }, "rank rejects out-of-range node");
    check(g.rankOfPath({2}).str() == "1", "single-node path ranks 1");

    // Big k beyond 64-bit range on the 70-node graph.
    Graph huge = completeForward(70);
    const BigInt bigTotal = huge.countPaths(0, 69); // 2^68
    const std::vector<int> last = huge.kthPath(0, 69, bigTotal);
    check((last == std::vector<int>{0, 69}), "largest-rank path is the direct edge path");
    check(huge.rankOfPath(last) == bigTotal, "rank of direct edge path equals total");
    const std::vector<int> first = huge.kthPath(0, 69, BigInt(1));
    check((first == std::vector<int>{0, 1, 2, 3, 4, 5, 6, 7, 8, 9,
                                      10, 11, 12, 13, 14, 15, 16, 17, 18, 19,
                                      20, 21, 22, 23, 24, 25, 26, 27, 28, 29,
                                      30, 31, 32, 33, 34, 35, 36, 37, 38, 39,
                                      40, 41, 42, 43, 44, 45, 46, 47, 48, 49,
                                      50, 51, 52, 53, 54, 55, 56, 57, 58, 59,
                                      60, 61, 62, 63, 64, 65, 66, 67, 68, 69}),
          "rank-1 path visits every node in id order");

    // rank/kth consistency on a medium random-ish graph: every enumerated
    // path must round-trip for all pairs (naive reference cross-check).
    Graph cross = Graph::build(7, {
        {0, 1}, {0, 2}, {0, 3}, {1, 2}, {1, 4}, {2, 3},
        {2, 5}, {3, 5}, {4, 5}, {4, 6}, {5, 6}
    });
    for (int s = 0; s < 7; ++s) {
        for (int t = s; t < 7; ++t) {
            auto paths = cross.enumeratePaths(s, t);
            check(cross.countPaths(s, t).str() == std::to_string(paths.size()),
                  "cross-check count " + std::to_string(s) + "->" + std::to_string(t));
            for (size_t i = 0; i < paths.size(); ++i) {
                const BigInt k(static_cast<uint64_t>(i + 1));
                check(cross.kthPath(s, t, k) == paths[i], "cross-check kth");
                check(cross.rankOfPath(paths[i]) == k, "cross-check rank");
            }
        }
    }
}

void testJson() {
    JsonValue v = parseJson(R"({"a":[1,true,"x"],"b":null})");
    check(v.isObject(), "JSON parses object");
    check(v.get("a")->arr.size() == 3, "JSON nested array");
    check(v.get("a")->arr[1].boolean, "JSON bool");
    check(v.get("a")->arr[2].str == "x", "JSON string");
    check(v.get("b")->type == JsonType::Null, "JSON null");
    check(v.get("missing") == nullptr, "JSON missing key");

    checkThrows([] { parseJson("{"); }, "JSON unterminated object rejected");
    checkThrows([] { parseJson("[1,]"); }, "JSON trailing comma rejected");
    checkThrows([] { parseJson(R"({"a":1}x)"); }, "JSON trailing chars rejected");

    const std::string escaped = parseJson(R"("a\nbA")").str;
    check(escaped == "a\nbA", "JSON escapes decode");

    // Round-trip a response through serialize + parse.
    JsonValue resp = errorResponse("X", "msg");
    JsonValue again = parseJson(resp.dump());
    check(again.get("ok")->boolean == false, "error response round-trips");
}

void testApi() {
    auto request = [](const std::string& raw) {
        return handleRequest(parseJson(raw));
    };

    JsonValue ok = request(R"({
        "action": "count",
        "graph": {"nodes": 4, "edges": [[0,1],[0,2],[1,3],[2,3]]},
        "source": 0, "target": 3
    })");
    check(ok.get("ok")->boolean, "API count success");
    check(ok.get("data")->get("paths")->str == "2", "API count value");

    JsonValue batch = request(R"({
        "action": "count",
        "graph": {"nodes": 4, "edges": [[0,1],[0,2],[1,3],[2,3]]},
        "source": [0, 1], "target": [2, 3]
    })");
    check(batch.get("data")->get("results")->arr.size() == 4, "API batch size");
    check(batch.get("data")->get("results")->arr[2].get("paths")->str == "0",
          "API batch includes unreachable pair (1 -> 2)");

    JsonValue kth = request(R"({
        "action": "kth",
        "graph": {"nodes": 4, "edges": [[0,1],[0,2],[1,3],[2,3]]},
        "source": 0, "target": 3, "k": 2
    })");
    check(kth.get("ok")->boolean, "API kth success");
    check(kth.get("data")->get("path")->arr[1].number == "2", "API kth second path");

    JsonValue kthOversize = request(R"({
        "action": "kth",
        "graph": {"nodes": 4, "edges": [[0,1],[0,2],[1,3],[2,3]]},
        "source": 0, "target": 3, "k": 3
    })");
    check(kthOversize.get("ok")->boolean == false, "API k out of range fails");
    check(kthOversize.get("error")->get("code")->str == "K_OUT_OF_RANGE",
          "API k error code");

    JsonValue unreachable = request(R"({
        "action": "kth",
        "graph": {"nodes": 3, "edges": [[0,1]]},
        "source": 1, "target": 0, "k": 1
    })");
    check(unreachable.get("error")->get("code")->str == "K_OUT_OF_RANGE",
          "API kth unreachable reports K_OUT_OF_RANGE");

    JsonValue rank = request(R"({
        "action": "rank",
        "graph": {"nodes": 4, "edges": [[0,1],[0,2],[1,3],[2,3]]},
        "path": [0, 2, 3]
    })");
    check(rank.get("data")->get("rank")->str == "2", "API rank value");

    JsonValue badPath = request(R"({
        "action": "rank",
        "graph": {"nodes": 4, "edges": [[0,1],[0,2],[1,3],[2,3]]},
        "path": [0, 3]
    })");
    check(badPath.get("error")->get("code")->str == "INVALID_PATH",
          "API invalid path code");

    JsonValue cycle = request(R"({
        "action": "count",
        "graph": {"nodes": 2, "edges": [[0,1],[1,0]]},
        "source": 0, "target": 1
    })");
    check(cycle.get("error")->get("code")->str == "INVALID_GRAPH",
          "API cycle rejected with INVALID_GRAPH");

    JsonValue badJson = handleRequest(parseJson("[]"));
    check(badJson.get("error")->get("code")->str == "INVALID_REQUEST",
          "API non-object request rejected");

    JsonValue enumResp = request(R"({
        "action": "enumerate",
        "graph": {"nodes": 4, "edges": [[0,1],[0,2],[1,3],[2,3]]},
        "source": 0, "target": 3
    })");
    check(enumResp.get("data")->get("count")->str == "2", "API enumerate count");
    check(enumResp.get("data")->get("paths")->arr.size() == 2,
          "API enumerate paths length");

    // Huge k accepted as a string.
    JsonValue hugeK = request(R"({
        "action": "kth",
        "graph": {"nodes": 3, "edges": [[0,1],[0,2],[1,2]]},
        "source": 0, "target": 2, "k": "2"
    })");
    check(hugeK.get("ok")->boolean && hugeK.get("data")->get("path")->arr[1].number == "2",
          "API k accepted as string");
}

} // namespace

int main() {
    testBigInt();
    testGraphValidation();
    testCounts();
    testEnumerationAndOrder();
    testKthAndRank();
    testJson();
    testApi();

    std::cout << (g_failures == 0 ? "ALL TESTS PASSED" : "SOME TESTS FAILED")
              << ": " << (g_checks - g_failures) << '/' << g_checks
              << " checks passed\n";
    return g_failures == 0 ? 0 : 1;
}
