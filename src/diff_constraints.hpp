// diff_constraints.hpp - Core solver for integer difference constraints.
//
// A constraint has the form   x - y <= c   (x, y integer variables, c integer).
// It is modeled as a directed edge  y -> x  with weight c in a constraint graph:
// a feasible integer assignment exists iff the graph has no negative cycle.
//
// Primary algorithm : Bellman-Ford from an implicit zero-weight super-source
//                      (all distance labels start at 0). Returns a feasible
//                      assignment or an explicit negative-cycle witness with
//                      the original constraint IDs.
// Naive reference    : Floyd-Warshall (O(n^3)) with the same super-source,
//                      cross-checked against Bellman-Ford on small inputs.
//
// No external solver/library is used for any of this.
#ifndef DIFFCON_DIFF_CONSTRAINTS_HPP
#define DIFFCON_DIFF_CONSTRAINTS_HPP

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <limits>
#include <queue>
#include <vector>

namespace diffcon {

constexpr int MAX_VARIABLES = 2000;
constexpr int MAX_CONSTRAINTS = 8000;
constexpr int64_t MAX_ABS_C = 1'000'000'000;  // |c| per constraint
constexpr int NAIVE_FLOYD_N_LIMIT = 300;      // Floyd is O(n^3); only run below this
constexpr int MINIMAL_CANDIDATE_N_LIMIT = 150;
constexpr int MINIMAL_CANDIDATE_M_LIMIT = 500;
constexpr int64_t INF64 = 4'000'000'000'000'000'000LL;

// One input constraint: x - y <= c.
struct Constraint {
  int x;        // head variable index
  int y;        // tail variable index
  int64_t c;    // bound
};

struct BellmanFordResult {
  bool feasible = true;
  std::vector<int64_t> dist;   // raw feasible labels (valid when feasible)
  int iterations = 0;          // relaxation rounds executed
  int64_t relaxations = 0;     // total successful relaxations
  // Negative-cycle witness (valid when !feasible):
  std::vector<int> cycleEdges;       // edge indices, in cycle direction
  std::vector<int> cycleVertices;    // vertices visited, start vertex repeated at end
  int64_t cycleWeight = 0;
};

struct FloydResult {
  bool used = false;           // false when above the O(n^3) size limit
  bool feasible = true;
  std::vector<int64_t> dist;   // feasible labels from super-source row
  int64_t elapsedMicros = 0;
  std::vector<int> cycleEdges;  // best-effort witness extraction
};

// Classic Bellman-Ford over an explicit edge list with an implicit super-source
// (all labels start at 0, so every component is reached without adding edges).
//
// Deterministic: edges are scanned in input order; ties are never relaxed.
inline BellmanFordResult bellmanFord(int n, const std::vector<Constraint>& edges) {
  BellmanFordResult r;
  r.dist.assign(n, 0);
  std::vector<int> predEdge(n, -1);
  std::vector<int> predNode(n, -1);

  int lastChanged = -1;
  for (int iter = 0; iter < n; ++iter) {
    r.iterations = iter + 1;
    bool changed = false;
    lastChanged = -1;
    for (int e = 0; e < static_cast<int>(edges.size()); ++e) {
      const Constraint& edge = edges[e];
      int64_t candidate = r.dist[edge.y] + edge.c;  // safe: |path| <= n * MAX_ABS_C
      if (candidate < r.dist[edge.x]) {
        r.dist[edge.x] = candidate;
        predEdge[edge.x] = e;
        predNode[edge.x] = edge.y;
        changed = true;
        ++r.relaxations;
        lastChanged = edge.x;
      }
    }
    if (!changed) {
      r.feasible = true;
      return r;  // early exit: labels converged
    }
  }

  // Round n changed nothing (covers n == 0 and the converged case): feasible.
  if (lastChanged == -1) {
    r.feasible = true;
    return r;
  }

  // A relaxation still happened in round n -> a reachable negative cycle exists.
  r.feasible = false;
  int x = lastChanged;
  for (int i = 0; i < n; ++i) x = predNode[x];  // walk n predecessors: x is on the cycle

  std::vector<int> cycleEdges;
  std::vector<int> cycleVertices;
  int cur = x;
  do {
    int e = predEdge[cur];
    cycleEdges.push_back(e);
    cycleVertices.push_back(cur);
    cur = predNode[cur];
  } while (cur != x);
  cycleVertices.push_back(x);
  std::reverse(cycleEdges.begin(), cycleEdges.end());
  std::reverse(cycleVertices.begin(), cycleVertices.end());

  int64_t weight = 0;
  for (int e : cycleEdges) weight += edges[e].c;
  r.cycleEdges = cycleEdges;
  r.cycleVertices = cycleVertices;
  r.cycleWeight = weight;
  return r;
}

// Feasibility-only Bellman-Ford on a masked edge subset (for candidate minimization).
inline bool maskedHasNegativeCycle(int n, const std::vector<Constraint>& edges,
                            const std::vector<char>& kept) {
  std::vector<int64_t> dist(n, 0);
  for (int iter = 0; iter < n; ++iter) {
    bool changed = false;
    for (int e = 0; e < static_cast<int>(edges.size()); ++e) {
      if (!kept[e]) continue;
      const Constraint& edge = edges[e];
      int64_t candidate = dist[edge.y] + edge.c;
      if (candidate < dist[edge.x]) {
        dist[edge.x] = candidate;
        changed = true;
      }
    }
    if (!changed) return false;
  }
  return true;
}

// Naive O(n^3) reference: Floyd-Warshall on n variables + one super-source s
// with zero edges s -> v. Feasible iff no diagonal entry is negative; the
// super-source row is simultaneously a feasible assignment.
inline FloydResult floydWarshall(int n, const std::vector<Constraint>& edges) {
  FloydResult r;
  if (n > NAIVE_FLOYD_N_LIMIT) {
    r.used = false;
    return r;
  }
  auto t0 = std::chrono::steady_clock::now();
  r.used = true;

  const int s = n;
  const int sz = n + 1;
  std::vector<int64_t> d(static_cast<size_t>(sz) * sz, INF64);
  std::vector<int> succV(static_cast<size_t>(sz) * sz, -1);
  std::vector<int> succE(static_cast<size_t>(sz) * sz, -1);
  auto at = [sz](int i, int j) { return static_cast<size_t>(i) * sz + j; };

  for (int i = 0; i < sz; ++i) d[at(i, i)] = 0;
  for (int v = 0; v < n; ++v) {
    d[at(s, v)] = 0;
    succV[at(s, v)] = v;  // super-source edge, no constraint id
  }
  for (int e = 0; e < static_cast<int>(edges.size()); ++e) {
    const Constraint& edge = edges[e];
    size_t k = at(edge.y, edge.x);
    if (edge.c < d[k]) {
      d[k] = edge.c;
      succV[k] = edge.x;
      succE[k] = e;
    }
  }

  for (int k = 0; k < sz; ++k) {
    for (int i = 0; i < sz; ++i) {
      if (d[at(i, k)] == INF64) continue;
      for (int j = 0; j < sz; ++j) {
        if (d[at(k, j)] == INF64) continue;
        int64_t via = d[at(i, k)] + d[at(k, j)];
        if (via < d[at(i, j)]) {
          d[at(i, j)] = via;
          succV[at(i, j)] = succV[at(i, k)];
          succE[at(i, j)] = succE[at(i, k)];
        }
      }
    }
  }

  int neg = -1;
  for (int i = 0; i < sz; ++i) {
    if (d[at(i, i)] < 0) { neg = i; break; }
  }
  r.feasible = (neg == -1);

  if (r.feasible) {
    r.dist.resize(n);
    for (int v = 0; v < n; ++v) r.dist[v] = d[at(s, v)];
  } else if (neg < n) {
    // Best-effort cycle reconstruction following successor pointers: neg -> ... -> neg.
    int cur = neg;
    int guard = 0;
    do {
      int e = succE[at(cur, neg)];
      int nxt = succV[at(cur, neg)];
      if (e < 0 || nxt < 0) { r.cycleEdges.clear(); break; }
      r.cycleEdges.push_back(e);
      cur = nxt;
    } while (cur != neg && ++guard <= n);
    if (cur != neg) r.cycleEdges.clear();
  }

  auto t1 = std::chrono::steady_clock::now();
  r.elapsedMicros = std::chrono::duration_cast<std::chrono::microseconds>(t1 - t0).count();
  return r;
}

// Undirected connected components over constraint endpoints; isolated
// variables are singleton components. Component order and member order follow
// (sorted) variable indices so output is deterministic.
inline std::vector<std::vector<int>> connectedComponents(int n, const std::vector<Constraint>& edges) {
  std::vector<std::vector<int>> adj(n);
  for (const Constraint& e : edges) {
    adj[e.x].push_back(e.y);
    adj[e.y].push_back(e.x);
  }
  std::vector<int> seen(n, -1);
  std::vector<std::vector<int>> comps;
  for (int start = 0; start < n; ++start) {
    if (seen[start] != -1) continue;
    int id = static_cast<int>(comps.size());
    std::vector<int> comp;
    std::queue<int> q;
    q.push(start);
    seen[start] = id;
    while (!q.empty()) {
      int v = q.front();
      q.pop();
      comp.push_back(v);
      for (int u : adj[v]) {
        if (seen[u] == -1) { seen[u] = id; q.push(u); }
      }
    }
    std::sort(comp.begin(), comp.end());
    comps.push_back(std::move(comp));
  }
  return comps;
}

// Deletion-filter minimal unsatisfiable subset candidate: drop each constraint
// (input order); keep it dropped iff the rest is still infeasible. The survivor
// set is infeasible and irreducible (every member is necessary).
// "Minimal", not "minimum": the result depends on input order.
struct MinimalCandidate {
  std::vector<int> edgeIndices;
  std::string method;  // "deletion_filter" or "cycle_witness_fallback"
  int checks = 0;      // feasibility checks performed while shrinking the set
};

inline MinimalCandidate minimalContradictionCandidate(int n, const std::vector<Constraint>& edges,
                                               const std::vector<int>& cycleEdges) {
  MinimalCandidate out;
  if (n > MINIMAL_CANDIDATE_N_LIMIT ||
      static_cast<int>(edges.size()) > MINIMAL_CANDIDATE_M_LIMIT) {
    out.method = "cycle_witness_fallback";
    out.edgeIndices = cycleEdges;  // a simple negative cycle is itself contradictory
    return out;
  }
  out.method = "deletion_filter";
  std::vector<char> kept(edges.size(), true);
  for (int e = 0; e < static_cast<int>(edges.size()); ++e) {
    kept[e] = false;
    ++out.checks;
    if (!maskedHasNegativeCycle(n, edges, kept)) kept[e] = true;  // still needed
  }
  for (int e = 0; e < static_cast<int>(edges.size()); ++e) {
    if (kept[e]) out.edgeIndices.push_back(e);
  }
  return out;
}

// Per-component translation normalization: shift every component so its minimum
// label becomes 0. Keeps connected variables' relative differences intact.
inline std::vector<int64_t> normalizePerComponent(const std::vector<int64_t>& raw,
                                           const std::vector<std::vector<int>>& comps) {
  std::vector<int64_t> out = raw;
  for (const auto& comp : comps) {
    int64_t shift = std::numeric_limits<int64_t>::max();
    for (int v : comp) shift = std::min(shift, raw[v]);
    for (int v : comp) out[v] = raw[v] - shift;
  }
  return out;
}

// Independent verifier: check x - y <= c for every constraint (also used by tests).
inline bool assignmentSatisfies(const std::vector<int64_t>& values,
                         const std::vector<Constraint>& edges,
                         int64_t* violatingEdge = nullptr) {
  for (int e = 0; e < static_cast<int>(edges.size()); ++e) {
    const Constraint& edge = edges[e];
    if (values[edge.x] - values[edge.y] > edge.c) {
      if (violatingEdge) *violatingEdge = e;
      return false;
    }
  }
  return true;
}

}  // namespace diffcon

#endif  // DIFFCON_DIFF_CONSTRAINTS_HPP
