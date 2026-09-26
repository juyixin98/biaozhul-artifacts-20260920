#include "solver.hpp"

#include <algorithm>

namespace diffc {

SolveResult solve(const Problem& p) {
  const int n = static_cast<int>(p.varNames.size());
  const int m = static_cast<int>(p.constraints.size());

  // dist[v]: best known upper bound on x_v relative to the super-source.
  std::vector<std::int64_t> dist(n, 0);
  std::vector<int> parentEdge(n, -1);

  int lastRelaxed = -1;
  for (int round = 0; round < n; ++round) {
    lastRelaxed = -1;
    for (int e = 0; e < m; ++e) {
      const Constraint& c = p.constraints[e];
      // Edge y -> x with weight c: dist[x] <= dist[y] + c.
      if (dist[c.x] > dist[c.y] + c.c) {
        dist[c.x] = dist[c.y] + c.c;
        parentEdge[c.x] = e;
        lastRelaxed = c.x;
      }
    }
    if (lastRelaxed == -1) break;  // converged early: no negative cycle
  }

  SolveResult res;

  if (lastRelaxed != -1) {
    // A relaxation succeeded in the n-th round => negative cycle exists.
    // Walking parent pointers n times from any still-relaxable vertex is
    // guaranteed to land inside a cycle.
    res.feasible = false;
    int v = lastRelaxed;
    for (int i = 0; i < n; ++i) v = p.constraints[parentEdge[v]].y;

    // Collect the cycle by following parents until we return to v.
    // Edge parentEdge[u] enters u, i.e. it is the constraint (u - y <= c).
    std::vector<int> cycleEdges;
    int u = v;
    do {
      int e = parentEdge[u];
      cycleEdges.push_back(e);
      u = p.constraints[e].y;
    } while (u != v);

    // cycleEdges currently runs against the edge direction; reverse so that
    // consecutive constraints chain: x_{i+1} == y_i ... in graph terms the
    // head of edge i equals the tail of edge i+1.
    std::reverse(cycleEdges.begin(), cycleEdges.end());

    std::int64_t weight = 0;
    for (int e : cycleEdges) weight += p.constraints[e].c;

    res.cycleConstraints = std::move(cycleEdges);
    res.cycleWeight = weight;
    for (int e : res.cycleConstraints)
      res.cycleVariables.push_back(p.constraints[e].x);
    return res;
  }

  // Feasible: dist is a valid assignment. Normalize by translation so the
  // minimum value is 0.
  res.feasible = true;
  std::int64_t mn = *std::min_element(dist.begin(), dist.end());
  res.assignment.resize(n);
  for (int i = 0; i < n; ++i) res.assignment[i] = dist[i] - mn;
  return res;
}

std::vector<int> findViolations(const Problem& p,
                                const std::vector<std::int64_t>& assignment) {
  std::vector<int> bad;
  for (size_t i = 0; i < p.constraints.size(); ++i) {
    const Constraint& c = p.constraints[i];
    if (assignment[c.x] - assignment[c.y] > c.c) bad.push_back(static_cast<int>(i));
  }
  return bad;
}

}  // namespace diffc
