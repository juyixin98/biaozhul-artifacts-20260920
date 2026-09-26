// Command-line JSON backend for treewidth elimination heuristics.
//
// Usage:
//   tdw_cli solve      < request.json   # min-fill heuristic + decomposition
//   tdw_cli exact      < request.json   # naive n! exact reference (n <= limit)
//   tdw_cli verify     < response.json  # independent validation of a response
//   tdw_cli exhaustive [--n 5]          # every labeled graph up to n
//   tdw_cli bench                       # capped-scale timing evidence
//
// All modes print JSON to stdout. No external solver is used anywhere.
#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <iostream>
#include <optional>
#include <random>
#include <sstream>
#include <string>
#include <vector>

#include "elimination.hpp"
#include "graph.hpp"
#include "json.hpp"
#include "tree_decomposition.hpp"
#include "validator.hpp"

namespace {

using namespace tdw;

constexpr int MAX_N = MAX_VERTICES;          // 64, bitset bound
constexpr int DEFAULT_EXACT_LIMIT = 8;       // 8! = 40320 replays
constexpr int HARD_EXACT_LIMIT = 10;          // 10! = 3628800 replays
const char* WIDTH_CLAIM =
    "heuristic_upper_bound_on_treewidth_not_claimed_optimal";

Json jnum(long long v) {
    Json j; j.type = Json::NUM; j.number = static_cast<double>(v); return j;
}
Json jstr(const std::string& s) {
    Json j; j.type = Json::STR; j.text = s; return j;
}
Json jbool(bool b) {
    Json j; j.type = Json::BOOL; j.boolean = b; return j;
}

Json pairArray(const std::vector<std::pair<int, int>>& edges) {
    Json a; a.type = Json::ARR;
    for (auto [u, v] : edges) {
        Json p; p.type = Json::ARR;
        p.items = {jnum(u), jnum(v)};
        a.items.push_back(std::move(p));
    }
    return a;
}

Json intArray(const std::vector<int>& xs) {
    Json a; a.type = Json::ARR;
    for (int x : xs) a.items.push_back(jnum(x));
    return a;
}

Json decompositionJson(const TreeDecomposition& td,
                       const std::vector<int>& order) {
    Json bags; bags.type = Json::ARR;
    for (size_t i = 0; i < td.bags.size(); ++i) {
        Json b; b.type = Json::OBJ;
        b.members = {
            {"id", jnum(static_cast<long long>(i))},
            {"eliminated_vertex", jnum(order[i])},
            {"vertices", intArray(td.bags[i])},
            {"bag_size", jnum(td.bags[i].size())}
        };
        bags.items.push_back(std::move(b));
    }
    Json o; o.type = Json::OBJ;
    o.members = {
        {"bags", std::move(bags)},
        {"bag_tree_edges", pairArray(td.bagTreeEdges)},
        {"width", jnum(td.width)}
    };
    return o;
}

Json limitsJson() {
    Json o; o.type = Json::OBJ;
    o.members = {
        {"max_vertices", jnum(MAX_N)},
        {"default_exact_n_limit", jnum(DEFAULT_EXACT_LIMIT)},
        {"hard_exact_n_limit", jnum(HARD_EXACT_LIMIT)},
        {"solver", jstr("self_contained_no_external_solver")}
    };
    return o;
}

std::string readAllStdin() {
    std::ostringstream ss;
    ss << std::cin.rdbuf();
    return ss.str();
}

// Assembles the shared response body used by solve/exact.
Json buildResponse(const Graph& g, const ElimResult& elim,
                   const std::string& mode,
                   std::chrono::steady_clock::time_point t0,
                   std::chrono::steady_clock::time_point t1,
                   const ExactResult* exact = nullptr) {
    TreeDecomposition td = buildDecomposition(g, elim.order);

    Json resp; resp.type = Json::OBJ;
    std::vector<std::pair<std::string, Json>> members;
    members.emplace_back("mode", jstr(mode));
    members.emplace_back("graph", graphToJson(g));
    members.emplace_back("heuristic", jstr("min_fill"));
    members.emplace_back("elimination_order", intArray(elim.order));
    members.emplace_back("heuristic_width", jnum(elim.width));
    members.emplace_back("width_claim", jstr(WIDTH_CLAIM));
    members.emplace_back("is_optimal", jbool(false));
    members.emplace_back("fill_edges", pairArray(elim.fillEdges));
    members.emplace_back("tree_decomposition", decompositionJson(td, elim.order));

    Json stats; stats.type = Json::OBJ;
    stats.members = {
        {"fill_edge_count", jnum(elim.fillEdges.size())},
        {"candidates_considered_per_step",
         intArray(elim.candidatesConsidered)},
        {"elapsed_milliseconds",
         jnum(std::chrono::duration_cast<std::chrono::milliseconds>(t1 - t0).count())},
        {"elapsed_microseconds",
         jnum(std::chrono::duration_cast<std::chrono::microseconds>(t1 - t0).count())}
    };
    members.emplace_back("statistics", std::move(stats));

    if (exact != nullptr) {
        members.emplace_back("optimal_width", jnum(exact->width));
        members.emplace_back("optimal_order", intArray(exact->order));
        members.emplace_back("exact_method", jstr("naive_permutation_enumeration"));
        members.emplace_back("permutations_examined",
                             jnum(static_cast<long long>(exact->permutationsExamined)));
        members.emplace_back("heuristic_gap",
                             jnum(elim.width - exact->width));
        members.emplace_back("heuristic_matches_optimum",
                             jbool(elim.width == exact->width));
    }
    members.emplace_back("limits", limitsJson());

    resp.members = std::move(members);

    // Independent validation is computed by the same code the standalone
    // `verify` mode uses, against the serialized response object.
    ValidationReport vr = validateResponse(resp);
    resp.members.emplace_back("validation", vr.toJson());
    return resp;
}

int runSolve(bool exactMode) {
    std::string raw = readAllStdin();
    Json req;
    try {
        req = Json::parse(raw);
    } catch (const std::exception& e) {
        std::cerr << "invalid JSON request: " << e.what() << "\n";
        return 2;
    }

    Graph g;
    try {
        g = parseGraph(req);
    } catch (const std::exception& e) {
        std::cerr << "invalid graph: " << e.what() << "\n";
        return 2;
    }

    int exactLimit = DEFAULT_EXACT_LIMIT;
    if (const Json* l = req.find("exact_n_limit")) {
        long long v;
        if (l->isIntegral(v) && v >= 0 && v <= HARD_EXACT_LIMIT) {
            exactLimit = static_cast<int>(v);
        }
    }

    auto t0 = std::chrono::steady_clock::now();
    ElimResult elim = minFillHeuristic(g);
    std::optional<ExactResult> exact;
    if (exactMode) {
        try {
            exact = exactOptimalWidth(g, exactLimit);
        } catch (const std::exception& e) {
            std::cerr << "exact mode refused: " << e.what() << "\n";
            return 2;
        }
    }
    auto t1 = std::chrono::steady_clock::now();

    Json resp = buildResponse(g, elim, exactMode ? "exact" : "heuristic",
                              t0, t1, exact ? &*exact : nullptr);
    std::cout << resp.dump(2) << "\n";
    return 0;
}

int runVerify() {
    std::string raw = readAllStdin();
    Json resp;
    try {
        resp = Json::parse(raw);
    } catch (const std::exception& e) {
        std::cerr << "invalid JSON: " << e.what() << "\n";
        return 2;
    }
    ValidationReport vr = validateResponse(resp);
    Json out; out.type = Json::OBJ;
    out.members = {
        {"verifier", jstr("independent_replay_and_running_intersection_check")},
        {"report", vr.toJson()}
    };
    std::cout << out.dump(2) << "\n";
    return vr.valid ? 0 : 1;
}

// -------- exhaustive sweep over all labeled simple graphs -----------------

uint64_t pairIndex(int n, int u, int v) {
    if (u > v) std::swap(u, v);
    // index of pair {u,v} among pairs with first < second
    return static_cast<uint64_t>(u) * (2 * n - u - 1) / 2 + (v - u - 1);
}

Graph graphFromCode(int n, uint64_t code) {
    Graph g;
    g.n = n;
    g.adj.assign(n, 0);
    for (int u = 0; u < n; ++u)
        for (int v = u + 1; v < n; ++v)
            if ((code >> pairIndex(n, u, v)) & 1ULL) {
                g.adj[u] |= 1ULL << v;
                g.adj[v] |= 1ULL << u;
            }
    return g;
}

int runExhaustive(int argc, char** argv) {
    int nMax = 5;
    bool useFast = false;
    for (int i = 2; i < argc; ++i) {
        std::string a = argv[i];
        if (a == "--n" && i + 1 < argc) nMax = std::atoi(argv[++i]);
        if (a == "--fast") useFast = true;
    }
    int hardCap = useFast ? 10 : 6;
    if (nMax < 1 || nMax > hardCap) {
        std::cerr << "exhaustive supports --n in [1," << hardCap << "]"
                  << " (naive n! reference needs n <= 6 for a full sweep; "
                  << "use --fast for the self-contained DFS exact)\n";
        return 2;
    }

    auto t0 = std::chrono::steady_clock::now();
    Json rows; rows.type = Json::ARR;
    uint64_t totalGraphs = 0;
    uint64_t totalGaps = 0;
    int worstGap = 0;
    Json examples; examples.type = Json::ARR;

    for (int n = 1; n <= nMax; ++n) {
        uint64_t pairs = static_cast<uint64_t>(n) * (n - 1) / 2;
        uint64_t count = 1ULL << pairs; // n<=6 -> at most 32768
        uint64_t gaps = 0;
        int rowWorst = 0;
        for (uint64_t code = 0; code < count; ++code) {
            Graph g = graphFromCode(n, code);
            ElimResult h = minFillHeuristic(g);
            int optWidth;
            std::vector<int> optOrder;
            uint64_t perms = 0;
            if (useFast) {
                optWidth = fastExactWidth(g, h.width);
            } else {
                ExactResult e = exactOptimalWidth(g, n); // n! reference
                optWidth = e.width;
                optOrder = e.order;
                perms = e.permutationsExamined;
            }
            int gap = h.width - optWidth;
            if (gap > 0) {
                ++gaps;
                if (gap > rowWorst && examples.items.size() < 10) {
                    Json ex; ex.type = Json::OBJ;
                    std::vector<std::pair<std::string, Json>> em = {
                        {"n", jnum(n)},
                        {"edges", pairArray([&] {
                            std::vector<std::pair<int, int>> es;
                            for (int u = 0; u < n; ++u)
                                for (int v = u + 1; v < n; ++v)
                                    if (g.hasEdge(u, v)) es.push_back({u, v});
                            return es;
                        }())},
                        {"heuristic_width", jnum(h.width)},
                        {"optimal_width", jnum(optWidth)},
                        {"heuristic_order", intArray(h.order)}
                    };
                    if (!useFast) em.push_back({"optimal_order", intArray(optOrder)});
                    ex.members = std::move(em);
                    examples.items.push_back(std::move(ex));
                }
            }
            rowWorst = std::max(rowWorst, gap);
            (void)perms;
        }
        totalGraphs += count;
        totalGaps += gaps;
        worstGap = std::max(worstGap, rowWorst);

        Json row; row.type = Json::OBJ;
        row.members = {
            {"n", jnum(n)},
            {"labeled_graphs_enumerated", jnum(static_cast<long long>(count))},
            {"graphs_where_minfill_exceeds_optimum",
             jnum(static_cast<long long>(gaps))},
            {"worst_gap", jnum(rowWorst)}
        };
        rows.items.push_back(std::move(row));
    }
    auto t1 = std::chrono::steady_clock::now();

    Json out; out.type = Json::OBJ;
    out.members = {
        {"method", jstr(useFast
            ? "all_labeled_simple_graphs_vs_self_contained_DFS_exact"
            : "all_labeled_simple_graphs_vs_n!_exact_reference")},
        {"exact_method", jstr(useFast
            ? "elimination_DFS_with_degeneracy_lower_bound"
            : "naive_permutation_enumeration")},
        {"external_solver_used", jbool(false)},
        {"n_max", jnum(nMax)},
        {"total_graphs", jnum(static_cast<long long>(totalGraphs))},
        {"total_graphs_with_suboptimal_heuristic",
         jnum(static_cast<long long>(totalGaps))},
        {"worst_gap_overall", jnum(worstGap)},
        {"suboptimal_examples", std::move(examples)},
        {"rows", std::move(rows)},
        {"elapsed_milliseconds",
         jnum(std::chrono::duration_cast<std::chrono::milliseconds>(t1 - t0).count())}
    };
    std::cout << out.dump(2) << "\n";
    return 0;
}

// ----------------------- capped-scale benchmark ---------------------------

Graph makeCycle(int n) {
    Graph g; g.n = n; g.adj.assign(n, 0);
    for (int i = 0; i < n; ++i) {
        int j = (i + 1) % n;
        g.adj[i] |= 1ULL << j;
        g.adj[j] |= 1ULL << i;
    }
    return g;
}

Graph makeClique(int n) {
    Graph g; g.n = n; g.adj.assign(n, ~0ULL);
    for (int i = 0; i < n; ++i) g.adj[i] &= ~(1ULL << i);
    if (n < 64) {
        uint64_t mask = (1ULL << n) - 1;
        for (int i = 0; i < n; ++i) g.adj[i] &= mask;
    }
    return g;
}

Graph makeDisjointPair(int n) {
    // Two equal cliques, disconnected: tests multi-root decomposition.
    Graph g; g.n = n; g.adj.assign(n, 0);
    int half = n / 2;
    for (int c = 0; c < 2; ++c) {
        int lo = c * half;
        int hi = c ? n : half;
        for (int u = lo; u < hi; ++u)
            for (int v = u + 1; v < hi; ++v) {
                g.adj[u] |= 1ULL << v;
                g.adj[v] |= 1ULL << u;
            }
    }
    return g;
}

Graph makeRandom(int n, double p, std::mt19937_64& rng) {
    Graph g; g.n = n; g.adj.assign(n, 0);
    std::uniform_real_distribution<double> uni(0.0, 1.0);
    for (int u = 0; u < n; ++u)
        for (int v = u + 1; v < n; ++v)
            if (uni(rng) < p) {
                g.adj[u] |= 1ULL << v;
                g.adj[v] |= 1ULL << u;
            }
    return g;
}

Json benchOne(const std::string& name, const Graph& g) {
    auto t0 = std::chrono::steady_clock::now();
    ElimResult h = minFillHeuristic(g);
    TreeDecomposition td = buildDecomposition(g, h.order);
    auto t1 = std::chrono::steady_clock::now();

    // Validate too, so timing includes evidence production.
    Json resp; resp.type = Json::OBJ;
    resp.members = {
        {"mode", jstr("heuristic")},
        {"graph", graphToJson(g)},
        {"heuristic", jstr("min_fill")},
        {"elimination_order", intArray(h.order)},
        {"heuristic_width", jnum(h.width)},
        {"width_claim", jstr(WIDTH_CLAIM)},
        {"is_optimal", jbool(false)},
        {"fill_edges", pairArray(h.fillEdges)},
        {"tree_decomposition", decompositionJson(td, h.order)}
    };
    ValidationReport vr = validateResponse(resp);

    Json o; o.type = Json::OBJ;
    o.members = {
        {"case", jstr(name)},
        {"n", jnum(g.n)},
        {"edges", jnum([&] {
            long long m = 0;
            for (int u = 0; u < g.n; ++u) m += __builtin_popcountll(g.adj[u]);
            return m / 2;
        }())},
        {"heuristic_width", jnum(h.width)},
        {"fill_edges", jnum(h.fillEdges.size())},
        {"decomposition_width", jnum(td.width)},
        {"independent_validation_valid", jbool(vr.valid)},
        {"elapsed_milliseconds",
         jnum(std::chrono::duration_cast<std::chrono::milliseconds>(t1 - t0).count())},
        {"elapsed_microseconds",
         jnum(std::chrono::duration_cast<std::chrono::microseconds>(t1 - t0).count())}
    };
    return o;
}

int runBench() {
    auto t0 = std::chrono::steady_clock::now();
    Json rows; rows.type = Json::ARR;
    std::mt19937_64 rng(0xC0FFEEULL);

    for (int n : {10, 20, 30, 40, 50, 60}) {
        rows.items.push_back(benchOne("cycle_n" + std::to_string(n), makeCycle(n)));
        rows.items.push_back(benchOne("clique_n" + std::to_string(n), makeClique(n)));
        rows.items.push_back(benchOne(
            "two_disjoint_cliques_n" + std::to_string(n), makeDisjointPair(n)));
        rows.items.push_back(benchOne(
            "random_p0.2_n" + std::to_string(n), makeRandom(n, 0.2, rng)));
    }
    auto t1 = std::chrono::steady_clock::now();

    Json out; out.type = Json::OBJ;
    out.members = {
        {"method", jstr("min_fill_heuristic_timing_not_optimal_treewidth")},
        {"max_vertices", jnum(MAX_N)},
        {"rows", std::move(rows)},
        {"total_elapsed_milliseconds",
         jnum(std::chrono::duration_cast<std::chrono::milliseconds>(t1 - t0).count())},
        {"total_elapsed_microseconds",
         jnum(std::chrono::duration_cast<std::chrono::microseconds>(t1 - t0).count())}
    };
    std::cout << out.dump(2) << "\n";
    return 0;
}

} // namespace

int main(int argc, char** argv) {
    std::string mode = argc > 1 ? argv[1] : "solve";
    try {
        if (mode == "solve") return runSolve(false);
        if (mode == "exact") return runSolve(true);
        if (mode == "verify") return runVerify();
        if (mode == "exhaustive") return runExhaustive(argc, argv);
        if (mode == "bench") return runBench();
        std::cerr << "unknown mode: " << mode
                  << " (expected solve|exact|verify|exhaustive|bench)\n";
        return 2;
    } catch (const std::exception& e) {
        std::cerr << "fatal: " << e.what() << "\n";
        return 2;
    }
}
