#include "reference.hpp"

#include <algorithm>

namespace diffc {

namespace {

// If a system of difference constraints is feasible, it has a solution with
// every value in [0, (n-1) * maxC]: take any shortest-path solution from the
// super-source (each value >= -(n-1)*maxC) and translate so the minimum is 0.
bool bruteForce(const Problem& p, const std::vector<const Constraint*>& cons,
                std::int64_t maxC) {
  const int n = static_cast<int>(p.varNames.size());
  const std::int64_t hi = static_cast<std::int64_t>(n > 0 ? n - 1 : 0) * maxC;

  std::vector<std::int64_t> x(n, 0);
  while (true) {
    bool ok = true;
    for (const Constraint* c : cons) {
      if (x[c->x] - x[c->y] > c->c) {
        ok = false;
        break;
      }
    }
    if (ok) return true;
    // Odometer increment over the box [0, hi]^n.
    int i = 0;
    for (; i < n; ++i) {
      if (x[i] < hi) {
        ++x[i];
        break;
      }
      x[i] = 0;
    }
    if (i == n) return false;  // wrapped around: whole box exhausted
  }
}

// Textbook Bellman-Ford: relax every edge n times, then report infeasible
// iff any edge can still be relaxed. No parent tracking, no cycle output.
bool plainRelaxation(const Problem& p, const std::vector<const Constraint*>& cons) {
  const int n = static_cast<int>(p.varNames.size());
  std::vector<std::int64_t> dist(n, 0);
  for (int round = 0; round < n; ++round) {
    for (const Constraint* c : cons) {
      if (dist[c->x] > dist[c->y] + c->c) dist[c->x] = dist[c->y] + c->c;
    }
  }
  for (const Constraint* c : cons) {
    if (dist[c->x] > dist[c->y] + c->c) return false;  // negative cycle
  }
  return true;
}

}  // namespace

ReferenceResult referenceCheck(const Problem& p, const std::vector<int>& subset) {
  std::vector<const Constraint*> cons;
  if (subset.empty()) {
    cons.reserve(p.constraints.size());
    for (const Constraint& c : p.constraints) cons.push_back(&c);
  } else {
    cons.reserve(subset.size());
    for (int i : subset) cons.push_back(&p.constraints[i]);
  }

  const int n = static_cast<int>(p.varNames.size());
  std::int64_t maxC = 0;
  for (const Constraint* c : cons) maxC = std::max(maxC, c->c < 0 ? -c->c : c->c);

  // Decide whether the brute-force box is small enough.
  const std::int64_t hi = static_cast<std::int64_t>(n > 0 ? n - 1 : 0) * maxC;
  // Box size (hi+1)^n with saturating arithmetic: once the count exceeds
  // the cap we only need to know that it is "too big".
  std::uint64_t combos = 1;
  const std::uint64_t base = static_cast<std::uint64_t>(hi) + 1;
  for (int i = 0; i < n && combos <= kBruteForceMaxCombos; ++i) {
    if (base != 0 && combos > kBruteForceMaxCombos / base) {
      combos = kBruteForceMaxCombos + 1;  // saturated: definitely too big
    } else {
      combos *= base;
    }
  }

  ReferenceResult res;
  if (combos <= kBruteForceMaxCombos) {
    res.usedBruteForce = true;
    res.feasible = bruteForce(p, cons, maxC);
  } else {
    res.usedBruteForce = false;
    res.feasible = plainRelaxation(p, cons);
  }
  return res;
}

}  // namespace diffc
