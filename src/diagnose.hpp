// Diagnose mode: minimal infeasible subset ("minimal contradiction set")
// candidates for an infeasible system.
//
// The negative cycle reported by the solver is already one minimal
// infeasible subset. For small systems (m <= kDiagnoseEnumMaxConstraints)
// we additionally enumerate subsets in increasing size, testing each with
// the naive reference checker, to find ALL minimal infeasible subsets —
// subject to the hard caps in model.hpp.
#pragma once

#include <cstdint>
#include <vector>

#include "model.hpp"

namespace diffc {

struct DiagnoseResult {
  bool feasible = true;
  // Each candidate is a set of constraint indices forming a minimal
  // infeasible subsystem (removing any one constraint makes it feasible).
  std::vector<std::vector<int>> candidates;
  bool enumerationPerformed = false;
  bool enumerationComplete = false;  // false => caps stopped the search
  std::uint64_t subsetsTested = 0;
};

DiagnoseResult diagnose(const Problem& p);

}  // namespace diffc
