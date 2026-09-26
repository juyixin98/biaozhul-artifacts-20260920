// DPLL solver with unit propagation, deterministic branching and a
// replayable search trace. No external SAT solver is used anywhere.
#pragma once

#include <cstddef>
#include <cstdint>
#include <stdexcept>
#include <vector>

#include "cnf.hpp"

namespace sat {

enum class Status { kSat, kUnsat };

// One step of the search trace. The independent checker replays these
// events against the CNF to verify the run.
struct Event {
  enum class Kind { kDecide, kUnit, kConflict, kBacktrack } kind;
  int var = 0;         // kDecide, kBacktrack
  bool value = false;  // kDecide
  Literal lit = 0;     // kUnit
  int clause = -1;     // kUnit, kConflict (index into Formula::clauses)
};

struct SolveResult {
  Status status = Status::kUnsat;
  // Index 1..num_vars; -1 = don't care (unassigned when SAT was decided).
  std::vector<int8_t> assignment;
  std::vector<Event> events;
};

// Thrown when the search exceeds the configured event budget; the caller
// reports "unknown" instead of claiming a result.
class LimitExceeded : public std::runtime_error {
 public:
  explicit LimitExceeded(const std::string& msg) : std::runtime_error(msg) {}
};

// Solves the (normalized) formula. Branching is deterministic: always the
// lowest-numbered unassigned variable, value false first, then true.
SolveResult dpll_solve(const Formula& formula, size_t max_events = 2000000);

}  // namespace sat
