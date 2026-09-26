#pragma once

#include <cstdint>
#include <string>
#include <utility>
#include <vector>

#include "graph.hpp"

namespace mcut {

// Every claim produced by the solver is re-derived from scratch here:
// the verifier only receives per-edge flows and the claimed source side,
// never the solver's residual graph.
struct VerifyReport {
  bool ok = true;
  std::vector<std::pair<std::string, bool>> checks;
  std::vector<std::string> failures;
  std::int64_t source_outflow = 0;   // net flow leaving s
  std::int64_t sink_inflow = 0;      // net flow entering t
  std::int64_t cut_value = 0;        // capacity of the reported cut

  void add(const std::string& name, bool passed, const std::string& detail = "") {
    checks.emplace_back(name, passed);
    if (!passed) {
      ok = false;
      failures.push_back(detail.empty() ? name : name + ": " + detail);
    }
  }
};

// `flows[i]` is the carried flow of problem.edges[i].
// `claimed_source_side` is the partition to verify.
// `claimed_flow_value` is the solver's reported max-flow value.
VerifyReport verify_solution(const Problem& problem,
                             const std::vector<std::int64_t>& flows,
                             const std::vector<unsigned char>& claimed_source_side,
                             std::int64_t claimed_flow_value);

}  // namespace mcut
