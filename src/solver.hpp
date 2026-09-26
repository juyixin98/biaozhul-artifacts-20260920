// Core solver: Bellman-Ford over the constraint graph.
// Constraint x - y <= c becomes a directed edge y -> x with weight c.
// A super-source with 0-weight edges to every vertex is simulated by
// initializing all distances to 0, so disconnected variables are handled
// uniformly. Implemented from scratch; no external solver is used.
#pragma once

#include <cstdint>
#include <vector>

#include "model.hpp"

namespace diffc {

struct SolveResult {
  bool feasible = false;

  // Feasible case: assignment[varIndex], translation-normalized so that
  // the minimum assigned value is 0 (adding a constant to every variable
  // never changes any x - y, so normalization preserves validity).
  std::vector<std::int64_t> assignment;

  // Infeasible case: one negative cycle, as indices into problem.constraints.
  // The cycle's constraints alone already form a minimal infeasible set:
  // any proper subset of a simple cycle is a path, hence feasible.
  std::vector<int> cycleConstraints;  // ordered along the cycle
  std::vector<int> cycleVariables;    // x-variable of each edge, in order
  std::int64_t cycleWeight = 0;       // sum of bounds along the cycle (< 0)
};

SolveResult solve(const Problem& p);

// Checks an assignment against all constraints; returns indices of violated
// constraints in input order.
std::vector<int> findViolations(const Problem& p,
                                const std::vector<std::int64_t>& assignment);

}  // namespace diffc
