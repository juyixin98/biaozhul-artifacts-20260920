// CNF formula types, validation and normalization.
#pragma once

#include <cstdint>
#include <vector>

namespace sat {

using Literal = int;  // nonzero: +v = variable v, -v = negation of v

struct Clause {
  std::vector<Literal> literals;
};

struct Formula {
  int num_vars = 0;
  std::vector<Clause> clauses;
};

// Hard limits. This is a small-scale teaching backend, not an industrial
// solver; requests beyond these bounds are rejected.
constexpr int kMaxVars = 64;
constexpr int kMaxClauses = 4096;
constexpr int kMaxClauseLength = 128;
constexpr int kMaxReferenceVars = 20;  // exhaustive reference enumeration

// Validates ranges and deduplicates literals inside each clause (first
// occurrence wins, order preserved). Tautological clauses are kept as-is:
// they are always satisfied and never unit/conflicting, so the solver and
// the checker handle them naturally.
// Throws std::invalid_argument on malformed input.
Formula normalize_formula(const Formula& raw);

bool clause_is_tautology(const Clause& clause);

// Value of a literal under a partial assignment:
//   1 = satisfied, 0 = falsified, -1 = unassigned.
// assignment[var] is -1 (unassigned), 0 (false) or 1 (true); index 0 unused.
int literal_value(const std::vector<int8_t>& assignment, Literal lit);

bool all_clauses_satisfied(const Formula& f,
                           const std::vector<int8_t>& assignment);

}  // namespace sat
