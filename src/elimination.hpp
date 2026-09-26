// Elimination orders: minimum-fill heuristic and a naive exact reference.
#pragma once

#include <cstdint>
#include <utility>
#include <vector>

#include "graph.hpp"

namespace tdw {

struct ElimResult {
    std::vector<int> order;      // Vertex ids in elimination order.
    int width = 0;               // max_i |later-neighbors of order[i]|.
    // Fill edges introduced during elimination (endpoints are original ids).
    std::vector<std::pair<int, int>> fillEdges;
    // Number of candidate vertices scanned while choosing each step (evidence
    // that the heuristic examined all remaining vertices).
    std::vector<int> candidatesConsidered;
};

// Minimum-fill heuristic: at each step eliminate a vertex whose elimination
// adds the fewest fill edges. Ties are broken by fewer current neighbors,
// then by the smallest vertex id (fully deterministic).
//
// This is a HEURISTIC: the returned width is an upper bound on the treewidth,
// not the optimum.
ElimResult minFillHeuristic(const Graph& g);

// Width obtained by a given permutation, replaying elimination independently
// (also counts fill edges). Used by the exact reference and by tests.
struct ReplayResult {
    int width = 0;
    std::vector<std::pair<int, int>> fillEdges;
};
ReplayResult replayOrder(const Graph& g, const std::vector<int>& order);

struct ExactResult {
    int width = 0;
    std::vector<int> order;
    uint64_t permutationsExamined = 0; // evidence of exhaustive work
};

// Naive exact optimum by enumerating every permutation (n! replays). The
// CLI enforces n <= 10; callers pick much smaller limits by default.
// No external solver is used.
ExactResult exactOptimalWidth(const Graph& g, int nLimit);

// ---- Fast exact primitives (still self-contained, no external solver) ----
//
// Clique number lower bound via subset enumeration (n <= ~20).
int maxCliqueSize(const Graph& g);

// Decision procedure: does an elimination ordering of width <= k exist?
// DFS over eliminations with a clique-number lower bound per state.
bool existsOrderWidthLE(std::vector<uint64_t> adj, uint64_t alive, int k);

// Fast exact optimum: binary / ascending search between clique number and an
// upper bound produced by the caller, using existsOrderWidthLE.
int fastExactWidth(const Graph& g, int upperBound);

} // namespace tdw
