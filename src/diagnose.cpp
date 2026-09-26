#include "diagnose.hpp"

#include <algorithm>

#include "reference.hpp"
#include "solver.hpp"

namespace diffc {

namespace {

bool containsCandidate(const std::vector<std::vector<int>>& found,
                       const std::vector<int>& subset) {
  // subset is a candidate only if no already-found minimal set is a
  // subsequence of it (both are sorted ascending).
  for (const auto& f : found) {
    if (f.size() > subset.size()) continue;
    if (std::includes(subset.begin(), subset.end(), f.begin(), f.end()))
      return true;
  }
  return false;
}

}  // namespace

DiagnoseResult diagnose(const Problem& p) {
  DiagnoseResult res;

  SolveResult s = solve(p);
  res.feasible = s.feasible;
  if (s.feasible) return res;

  // Candidate 1: the negative cycle from the core solver (sorted for
  // uniform comparison and output).
  std::vector<int> cycle = s.cycleConstraints;
  std::sort(cycle.begin(), cycle.end());
  res.candidates.push_back(cycle);

  const int m = static_cast<int>(p.constraints.size());
  if (m > static_cast<int>(kDiagnoseEnumMaxConstraints)) {
    return res;  // too large to enumerate; cycle candidate still returned
  }

  res.enumerationPerformed = true;
  res.enumerationComplete = true;

  // Enumerate subsets in increasing size; a subset is minimal infeasible iff
  // it is infeasible and contains no previously found minimal subset.
  std::vector<int> current;
  std::uint64_t budget = kDiagnoseMaxSubsetTests;

  for (int size = 1; size <= m; ++size) {
    // Any set larger than every found candidate and containing one is
    // skipped by containsCandidate, so enumeration stays exact.
    current.assign(static_cast<size_t>(size), 0);
    for (int i = 0; i < size; ++i) current[static_cast<size_t>(i)] = i;

    while (true) {
      if (budget == 0 ||
          res.candidates.size() >= kDiagnoseMaxCandidates) {
        res.enumerationComplete = false;
        return res;
      }
      if (!containsCandidate(res.candidates, current)) {
        --budget;
        ++res.subsetsTested;
        if (!referenceCheck(p, current).feasible) {
          res.candidates.push_back(current);
        }
      }
      // Next combination of `size` indices out of m (lexicographic).
      int i = size - 1;
      while (i >= 0 && current[static_cast<size_t>(i)] == m - size + i) --i;
      if (i < 0) break;
      ++current[static_cast<size_t>(i)];
      for (int j = i + 1; j < size; ++j)
        current[static_cast<size_t>(j)] = current[static_cast<size_t>(j - 1)] + 1;
    }
  }
  return res;
}

}  // namespace diffc
