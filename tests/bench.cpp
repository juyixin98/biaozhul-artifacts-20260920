// Microbenchmark for the core incremental algorithm (no JSON protocol).
//
// Reproducible workload: n vertices, m edge attempts drawn from a fixed
// LCG stream, forward-biased 80/20 so most inserts are trivial but a mix of
// reorders, duplicates and cycle rejections occurs. Prints accept /
// duplicate / cycle counts, actual visited-node totals are exercised by the
// test suite; here we report wall time and throughput, then independently
// validate the final order.
//
//   ./build/bench [n] [m] [seed]
#include <algorithm>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <vector>

#include "../src/topo.hpp"

using namespace topo;

namespace {

struct Rng {
    unsigned long long s;
    explicit Rng(unsigned long long x) : s(x ? x : 0x9e3779b97f4a7c15ULL) {}
    unsigned next() {
        s = s * 6364136223846793005ULL + 1442695040888963407ULL;
        return static_cast<unsigned>(s >> 33);
    }
    int below(int n) { return n <= 0 ? 0 : static_cast<int>(next() % static_cast<unsigned>(n)); }
};

} // namespace

int main(int argc, char** argv) {
    int n = (argc > 1) ? std::atoi(argv[1]) : 100000;
    int m = (argc > 2) ? std::atoi(argv[2]) : 200000;
    unsigned seed = (argc > 3) ? static_cast<unsigned>(std::atoll(argv[3])) : 7u;

    Rng rng(seed);
    std::vector<std::pair<int, int>> attempts;
    attempts.reserve(m);
    for (int k = 0; k < m; ++k) {
        int u = rng.below(n);
        int v = rng.below(n);
        if (rng.below(100) < 80 && u > v) std::swap(u, v);
        attempts.emplace_back(u, v);
    }

    IncrementalTopo t(n);
    long long accepted = 0, duplicates = 0, cycles = 0, visited = 0, reordered = 0;

    auto t0 = std::chrono::steady_clock::now();
    for (auto [u, v] : attempts) {
        InsertResult r = t.insertEdge(u, v);
        if (r.duplicate) ++duplicates;
        else if (r.cycle) ++cycles;
        else {
            ++accepted;
            if (r.reordered) {
                ++reordered;
                visited += r.visited;
            }
        }
    }
    auto t1 = std::chrono::steady_clock::now();
    double sec = std::chrono::duration<double>(t1 - t0).count();

    // Independent validation over exactly the committed edge set.
    Graph g(n);
    for (auto [u, v] : attempts)
        if (u != v && t.hasEdge(u, v)) g.addEdge(u, v);
    std::string err = validateOrder(g, t.order());

    std::printf("n=%d attempts=%d seed=%u\n", n, m, seed);
    std::printf("accepted=%lld duplicates=%lld cyclesRejected=%lld edges=%zu\n",
                accepted, duplicates, cycles, t.edgeCount());
    std::printf("reordered=%lld visitedNodes=%lld avgVisitedPerReorder=%.2f\n",
                reordered, visited,
                reordered ? static_cast<double>(visited) / reordered : 0.0);
    std::printf("time=%.3fs throughput=%.0f attempts/s\n", sec, m / sec);
    std::printf("independentValidation=%s\n", err.empty() ? "OK" : ("FAIL: " + err).c_str());
    return err.empty() ? 0 : 1;
}
