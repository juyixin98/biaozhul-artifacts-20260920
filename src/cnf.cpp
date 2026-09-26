#include "cnf.hpp"

#include <stdexcept>
#include <string>

namespace sat {

Formula normalize_formula(const Formula& raw) {
  if (raw.num_vars < 0 || raw.num_vars > kMaxVars) {
    throw std::invalid_argument("num_vars must be in [0, " +
                                std::to_string(kMaxVars) + "]");
  }
  if (raw.clauses.size() > static_cast<size_t>(kMaxClauses)) {
    throw std::invalid_argument("too many clauses (max " +
                                std::to_string(kMaxClauses) + ")");
  }
  Formula out;
  out.num_vars = raw.num_vars;
  out.clauses.reserve(raw.clauses.size());
  for (size_t i = 0; i < raw.clauses.size(); ++i) {
    const Clause& c = raw.clauses[i];
    if (c.literals.size() > static_cast<size_t>(kMaxClauseLength)) {
      throw std::invalid_argument("clause " + std::to_string(i) +
                                  " too long (max " +
                                  std::to_string(kMaxClauseLength) + ")");
    }
    Clause norm;
    for (Literal lit : c.literals) {
      if (lit == 0) {
        throw std::invalid_argument("clause " + std::to_string(i) +
                                    " contains literal 0");
      }
      int var = lit < 0 ? -lit : lit;
      if (var > raw.num_vars) {
        throw std::invalid_argument("clause " + std::to_string(i) +
                                    " references variable " +
                                    std::to_string(var) + " > num_vars");
      }
      bool seen = false;
      for (Literal kept : norm.literals) {
        if (kept == lit) {
          seen = true;
          break;
        }
      }
      if (!seen) norm.literals.push_back(lit);
    }
    out.clauses.push_back(std::move(norm));
  }
  return out;
}

bool clause_is_tautology(const Clause& clause) {
  for (Literal a : clause.literals) {
    for (Literal b : clause.literals) {
      if (a == -b) return true;
    }
  }
  return false;
}

int literal_value(const std::vector<int8_t>& assignment, Literal lit) {
  int var = lit < 0 ? -lit : lit;
  int8_t value = assignment[static_cast<size_t>(var)];
  if (value < 0) return -1;
  bool is_true = (value == 1);
  bool wanted = lit > 0;
  return is_true == wanted ? 1 : 0;
}

bool all_clauses_satisfied(const Formula& f,
                           const std::vector<int8_t>& assignment) {
  for (const Clause& c : f.clauses) {
    bool satisfied = false;
    for (Literal lit : c.literals) {
      if (literal_value(assignment, lit) == 1) {
        satisfied = true;
        break;
      }
    }
    if (!satisfied) return false;
  }
  return true;
}

}  // namespace sat
