// Exhaustive small-graph evidence tool (not part of the library).
//
// Enumerates EVERY labeled simple graph on n vertices (all 2^(n choose 2)
// edge subsets) and, for each one:
//   - computes the min-fill / min-degree heuristic widths,
//   - computes the optimum via the memoized exact search,
//   - (n <= 6) cross-checks the optimum against naive n! enumeration,
//   - counts graphs where each heuristic is strictly suboptimal,
//   - prints the first min-fill counterexample as JSON.
//
// This is how examples/request_heuristic_nonoptimal.json was produced.
// Build: make exhaustive ; build/exhaustive [nmax=7]
#include <iostream>
#include <string>
#include <vector>

#include "graph.hpp"
#include "json.hpp"
#include "treewidth.hpp"

namespace {

Graph fromEdgeMask(int n, unsigned long long em) {
    Graph g;
    g.labels.resize(n);
    for (int i = 0; i < n; ++i) g.labels[i] = std::to_string(i);
    g.adj.assign(n, std::vector<char>(n, 0));
    int bit = 0;
    for (int i = 0; i < n; ++i) {
        for (int j = i + 1; j < n; ++j, ++bit) {
            if (em & (1ULL << bit)) g.adj[i][j] = g.adj[j][i] = 1;
        }
    }
    return g;
}

}  // namespace

int main(int argc, char** argv) {
    int nmax = argc > 1 ? std::stoi(argv[1]) : 7;
    if (nmax > MAX_EXACT_N_HARD) {
        std::cerr << "exact search hard limit is " << MAX_EXACT_N_HARD << "\n";
        return 2;
    }

    for (int n = 1; n <= nmax; ++n) {
        int pairs = n * (n - 1) / 2;
        unsigned long long total = 1ULL << pairs;
        long long fillBad = 0, degreeBad = 0, disagree = 0;
        unsigned long long firstBadMask = 0;
        bool haveFirst = false;

        for (unsigned long long em = 0; em < total; ++em) {
            Graph g = fromEdgeMask(n, em);
            int hf = minFillOrder(g).width;
            int hd = minDegreeOrder(g).width;
            ExactResult ex = exactOptimal(g, MAX_EXACT_N_HARD);
            int opt = ex.width;

            if (n <= 6) {
                NaiveResult nv = naiveOptimal(g);
                if (nv.width != opt) {
                    ++disagree;
                    std::cerr << "exact/naive disagree at n=" << n
                              << " mask=" << em << "\n";
                }
            }
            if (hf > opt) {
                ++fillBad;
                if (!haveFirst) { haveFirst = true; firstBadMask = em; }
            }
            if (hd > opt) ++degreeBad;
        }

        std::cout << "n=" << n << ": " << total << " graphs | "
                  << "min-fill suboptimal: " << fillBad
                  << " | min-degree suboptimal: " << degreeBad
                  << " | exact!=naive: " << disagree << "\n";

        if (haveFirst && n == nmax) {
            Graph g = fromEdgeMask(n, firstBadMask);
            json::Value::ArrayT edges;
            for (int i = 0; i < n; ++i)
                for (int j = i + 1; j < n; ++j)
                    if (g.adj[i][j])
                        edges.push_back(json::Value::ArrayT{
                            json::Value(i), json::Value(j)});
            json::Value::ObjectT gobj;
            gobj.emplace("n", json::Value(n));
            gobj.emplace("edges", json::Value(std::move(edges)));
            json::Value::ObjectT req;
            req.emplace("action", json::Value("solve"));
            req.emplace("exact_limit", json::Value(n));
            req.emplace("graph", json::Value(std::move(gobj)));
            std::cout << "first min-fill counterexample request:\n"
                      << json::dump(json::Value(std::move(req)), 2);
        }
    }
    return 0;
}
