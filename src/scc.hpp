#pragma once

#include "graph.hpp"

// Core solver. No external graph/SAT/solver library is used: the SCC
// computation is a hand-written iterative Kosaraju (two DFS passes),
// witnesses come from BFS path search, and the condensation is built
// directly from edge multiplicities.
namespace scc {

AnalysisResult analyze(const Graph& g);

} // namespace scc
