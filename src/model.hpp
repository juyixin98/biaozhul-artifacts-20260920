// Problem model: integer difference constraints of the form  x - y <= c.
// Also defines the hard scale limits enforced at the input boundary.
#pragma once

#include <cstdint>
#include <string>
#include <vector>

#include "json.hpp"

namespace diffc {

// Hard input limits (documented in README; enforced while parsing).
constexpr size_t kMaxVariables = 5000;
constexpr size_t kMaxConstraints = 20000;
constexpr std::int64_t kMaxAbsBound = 1000000000LL;  // |c| <= 1e9

// Diagnose-mode enumeration caps (keeps the naive subset search bounded).
constexpr size_t kDiagnoseEnumMaxConstraints = 18;   // enumerate only if m <= this
constexpr size_t kDiagnoseMaxCandidates = 64;        // stop after this many hits
constexpr std::uint64_t kDiagnoseMaxSubsetTests = 200000;  // feasibility tests cap

struct Constraint {
  std::string id;   // unique constraint id (auto-assigned "c<index>" if absent)
  int x = -1;       // variable index on the left  (x ...)
  int y = -1;       // variable index subtracted  (... - y)
  std::int64_t c = 0;  // bound: x - y <= c
};

struct Problem {
  std::vector<std::string> varNames;
  std::vector<Constraint> constraints;
};

struct InputError {
  std::string message;
};

// Parses and validates a problem from a JSON object of the form:
//   { "variables": ["a", "b", ...],
//     "constraints": [ {"id": "c1", "var": "a", "minus": "b", "bound": 5}, ... ] }
// Throws InputError on any schema/semantic violation.
Problem parseProblem(const Json& root);

}  // namespace diffc
