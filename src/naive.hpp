// Naive reference implementation used to cross-check the production
// solver on small graphs.
//
// Strategy: enumerate ALL simple entry->v paths (DFS with a visited set)
// and intersect the node sets along them. d dominates v iff d appears in
// every such path. idom and dominance frontiers are then derived directly
// from the definitions. This is exponential in the worst case, so it is
// guarded by hard limits and only used for verification.
#pragma once

#include <string>
#include <vector>

#include "dom.hpp"
#include "graph.hpp"

namespace domtree {

// Safety limits for the exponential reference.
constexpr int kNaiveMaxNodes = 64;
constexpr long long kNaiveMaxPaths = 2000000;

struct NaiveResult {
  std::vector<bool> reachable;
  std::vector<int> idom;
  std::vector<std::vector<int>> frontier;
  long long paths_enumerated = 0;
};

// Returns false with `error` when limits are exceeded.
bool naive_solve(const Graph& g, int entry, NaiveResult& out,
                 std::string& error);

// Compares production vs naive results on reachable nodes. Returns a
// list of human-readable mismatch descriptions (empty when equal).
std::vector<std::string> compare_results(const Graph& g,
                                         const SolverResult& prod,
                                         const NaiveResult& ref);

}  // namespace domtree
