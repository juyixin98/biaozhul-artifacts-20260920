#pragma once

#include "graph.hpp"

// Naive reference implementations, intentionally simple and independent of
// the Kosaraju solver. Only for small graphs (n <= limits::MAX_NAIVE_N).
namespace naive {

// Boolean reachability matrix R where R[u*n+v] = true iff v is reachable
// from u (paths of length >= 0, so the diagonal is always true).
// Computed with Floyd-Warshall transitive closure: O(n^3).
std::vector<char> reachabilityMatrix(const Graph& g);

// Naive SCC partition: u,v are in the same SCC iff mutually reachable.
// Component ids follow the same canonical rule (ascending minimum vertex).
std::vector<int> sccByReachability(const Graph& g);

} // namespace naive
