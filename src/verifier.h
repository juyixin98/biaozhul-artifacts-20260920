// Independent verifier for max-flow / min-cut evidence.
//
// Given the solver's JSON response it checks every claim from scratch
// WITHOUT calling the solver:
//
//   C1 capacities: 0 <= flow(e) <= capacity(e) for every input edge
//   C2 conservation: net flow is zero at every node except s and t
//   C3 balance: net outflow(s) == net inflow(t) == reported max_flow >= 0
//   C4 partition: source in S, sink in T, S union T = all nodes
//   C5 cut edges: reported cut set is EXACTLY the input edges crossing S->T
//   C6 cut value: sum of capacities of cut edges == reported cut value
//   C7 equality: cut value == max flow  (strong max-flow/min-cut theorem)
//   C8 saturation: every S->T edge saturated, every T->S edge flow-free,
//                  i.e. no residual arc leaves S (independently BFS-verified)
//   C9 residual listing: every input edge has its own forward arc and its
//                  own artificial reverse arc, stored as separate arcs even
//                  when the input contains an edge in the opposite direction
#pragma once

#include <string>
#include <vector>

#include "minjson.h"

namespace mincut {

struct VerificationReport {
  bool valid = false;
  std::vector<std::string> errors;
  // Derived quantities, for evidence output.
  long long source_outflow = 0;
  long long sink_inflow = 0;
  long long cut_value_computed = 0;
  long long residual_reachable_count = 0;

  void fail(std::string message) {
    valid = false;
    errors.push_back(std::move(message));
  }
};

// Verifies a solver response document. Returns a report whose `valid` flag
// and error list fully describe the outcome.
VerificationReport verifyResponse(const minjson::Value& response);

// Convenience: builds a JSON object with the report fields.
minjson::Value reportToJson(const VerificationReport& report);

}  // namespace mincut
