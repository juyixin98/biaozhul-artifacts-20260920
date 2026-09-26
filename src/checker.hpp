// Independent checker. It does not reuse solver code: it replays the
// emitted search trace event by event against the CNF and verifies that
//   - every unit propagation is justified (clause is really unit),
//   - every conflict is justified (clause is really falsified),
//   - decisions follow the deterministic rule (lowest unassigned variable,
//     false branch first, true branch only after the false subtree failed),
//   - propagation is exhaustive before each decision (no pending unit or
//     falsified clause, formula not already satisfied),
//   - a SAT claim ends with all clauses satisfied,
//   - an UNSAT claim fully explores both branches of the root variable
//     (or fails at level 0 without any decision).
#pragma once

#include <cstdint>
#include <string>
#include <vector>

#include "cnf.hpp"
#include "dpll.hpp"

namespace sat {

struct CheckResult {
  bool ok = false;
  std::string error;
};

// Verifies that a (possibly partial, -1 = don't care) assignment satisfies
// every clause. assignment has size num_vars + 1, index 0 unused.
CheckResult check_assignment(const Formula& formula,
                             const std::vector<int8_t>& assignment);

// Replays the trace and verifies it proves the claimed status.
CheckResult verify_proof(const Formula& formula,
                         const std::vector<Event>& events, Status claimed);

}  // namespace sat
