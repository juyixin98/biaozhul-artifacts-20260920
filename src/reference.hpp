// Naive small-scale reference implementation, deliberately written in the
// most obvious way possible and independent of the main solver. Used to
// cross-check the optimized solver in tests and to power diagnose mode.
//
// Two strategies:
//  1. Brute force: for tiny systems, enumerate every integer assignment in
//     a provably sufficient bounded box and test all constraints directly.
//  2. Plain relaxation: textbook Bellman-Ford with no parent tracking and
//     no early-cycle extraction, used when the box is too large.
#pragma once

#include <cstdint>
#include <vector>

#include "model.hpp"

namespace diffc {

// Cap on the number of assignments the brute force will enumerate.
constexpr std::uint64_t kBruteForceMaxCombos = 2000000;

struct ReferenceResult {
  bool feasible = false;
  bool usedBruteForce = false;  // false => fell back to plain relaxation
};

// Feasibility check over a subset of constraints (subset = indices into
// problem.constraints; empty subset means "all constraints").
ReferenceResult referenceCheck(const Problem& p, const std::vector<int>& subset);

}  // namespace diffc
