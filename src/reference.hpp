// Naive exhaustive reference solver: enumerates all 2^n assignments.
// Used as the ground-truth oracle for small formulas (n <= 20).
#pragma once

#include <cstdint>
#include <vector>

#include "cnf.hpp"
#include "dpll.hpp"

namespace sat {

struct ReferenceResult {
  Status status = Status::kUnsat;
  // First satisfying assignment in enumeration order (var 1 = LSB of the
  // enumeration mask); empty when UNSAT. Index 1..num_vars.
  std::vector<int8_t> assignment;
  unsigned long long num_satisfying = 0;
};

// Throws std::invalid_argument when num_vars > kMaxReferenceVars.
ReferenceResult reference_solve(const Formula& formula);

}  // namespace sat
