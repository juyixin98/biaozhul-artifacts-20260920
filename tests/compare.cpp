// Small-scale comparison: incremental PK vs the naive full-recompute
// reference on identical edge streams. Asserts every accept/cycle/duplicate
// decision is identical and reports wall-time of each. This is the auditable
// evidence that the incremental solver agrees with recomputing Kahn from
// scratch after every insertion.
//
//   ./build/compare [n] [attempts] [seed]
#include <algorithm>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <vector>

#include "../src/naive_topo.hpp"
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
    int n = (argc > 1) ? std::atoi(argv[1]) : 300;
    int attempts = (argc > 2) ? std::atoi(argv[2]) : 3000;
    unsigned seed = (argc > 3) ? static_cast<unsigned>(std::atoll(argv[3])) : 20260925u;

    Rng rng(seed);
    std::vector<std::pair<int, int>> stream;
    stream.reserve(attempts);
    for (int k = 0; k < attempts; ++k)
        stream.emplace_back(rng.below(n), rng.below(n));

    // 1) Correctness: drive both on the identical stream, compare decisions.
    IncrementalTopo inc(n);
    NaiveTopo naive(n);

    long long mismatches = 0, accepted = 0, duplicates = 0, cycles = 0;
    for (auto [u, v] : stream) {
        NaiveInsertResult rn = naive.insertEdge(u, v);
        InsertResult ri = inc.insertEdge(u, v);
        if (ri.accepted != rn.accepted || ri.cycle != rn.cycle ||
            ri.duplicate != rn.duplicate ||
            inc.edgeCount() != naive.edgeCount()) {
            ++mismatches;
            std::printf("MISMATCH on %d->%d: inc(acc=%d cyc=%d dup=%d) "
                        "naive(acc=%d cyc=%d dup=%d)\n",
                        u, v, ri.accepted, ri.cycle, ri.duplicate,
                        rn.accepted, rn.cycle, rn.duplicate);
        }
        if (ri.duplicate) ++duplicates;
        else if (ri.cycle) ++cycles;
        else ++accepted;
    }
    std::string err = validateOrder(naive.graph(), inc.order());

    // 2) Timed incremental on a fresh instance.
    IncrementalTopo incTimed(n);
    auto t0 = std::chrono::steady_clock::now();
    for (auto [u, v] : stream) incTimed.insertEdge(u, v);
    auto t1 = std::chrono::steady_clock::now();

    // 3) Timed naive full-recompute on a fresh instance.
    NaiveTopo naiveTimed(n);
    auto t2 = std::chrono::steady_clock::now();
    for (auto [u, v] : stream) naiveTimed.insertEdge(u, v);
    auto t3 = std::chrono::steady_clock::now();

    double secInc = std::chrono::duration<double>(t1 - t0).count();
    double secNaive = std::chrono::duration<double>(t3 - t2).count();

    std::printf("n=%d attempts=%d seed=%u\n", n, attempts, seed);
    std::printf("accepted=%lld duplicates=%lld cyclesRejected=%lld\n",
                accepted, duplicates, cycles);
    std::printf("decisionMismatches=%lld incrementalOrderValid=%s\n",
                mismatches, err.empty() ? "yes" : ("NO: " + err).c_str());
    std::printf("timeIncremental=%.4fs timeNaiveFullRecompute=%.4fs speedup=%.1fx\n",
                secInc, secNaive, secNaive / secInc);

    return (mismatches == 0 && err.empty()) ? 0 : 1;
}
