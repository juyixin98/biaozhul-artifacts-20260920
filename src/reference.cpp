#include "reference.hpp"

#include <stdexcept>

namespace sat {

ReferenceResult reference_solve(const Formula& formula) {
  if (formula.num_vars > kMaxReferenceVars) {
    throw std::invalid_argument("reference solver limited to " +
                                std::to_string(kMaxReferenceVars) +
                                " variables");
  }
  ReferenceResult result;
  const unsigned long long total = 1ULL << formula.num_vars;
  std::vector<int8_t> assignment(static_cast<size_t>(formula.num_vars) + 1, -1);
  for (unsigned long long mask = 0; mask < total; ++mask) {
    for (int v = 1; v <= formula.num_vars; ++v) {
      assignment[static_cast<size_t>(v)] =
          (mask >> (v - 1)) & 1ULL ? 1 : 0;
    }
    if (all_clauses_satisfied(formula, assignment)) {
      ++result.num_satisfying;
      if (result.status == Status::kUnsat) {
        result.status = Status::kSat;
        result.assignment = assignment;
      }
    }
  }
  return result;
}

}  // namespace sat
