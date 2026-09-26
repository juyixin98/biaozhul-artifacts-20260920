#include "verifier.hpp"

#include <queue>

namespace mcut {

namespace {

struct ResArc {
  int to;
  std::int64_t residual;
};

}  // namespace

VerifyReport verify_solution(const Problem& problem,
                             const std::vector<std::int64_t>& flows,
                             const std::vector<unsigned char>& claimed_source_side,
                             std::int64_t claimed_flow_value) {
  VerifyReport r;
  const int n = problem.num_vertices;
  const int s = problem.source;
  const int t = problem.sink;
  const auto& edges = problem.edges;

  r.add("flow_count_matches_edges",
        static_cast<int>(flows.size()) == static_cast<int>(edges.size()),
        "got " + std::to_string(flows.size()) + " flows for " +
            std::to_string(edges.size()) + " edges");
  r.add("partition_covers_all_vertices",
        static_cast<int>(claimed_source_side.size()) == n,
        "partition has size " + std::to_string(claimed_source_side.size()) +
            ", graph has " + std::to_string(n) + " vertices");
  if (!r.ok) return r;  // subsequent checks would index out of range

  // 1. Capacity constraints: 0 <= f(e) <= c(e) for every edge.
  bool capacity_ok = true;
  for (int i = 0; i < static_cast<int>(edges.size()); ++i) {
    if (flows[i] < 0 || flows[i] > edges[i].capacity) {
      capacity_ok = false;
      r.add("capacity_constraints", false,
            "edge '" + edges[i].id + "' flow " + std::to_string(flows[i]) +
                " outside [0," + std::to_string(edges[i].capacity) + "]");
      break;
    }
  }
  if (capacity_ok) r.add("capacity_constraints", true);

  // 2. Flow conservation (per vertex, parallel edges counted separately):
  //    sum of outgoing flow == sum of incoming flow for v not in {s,t}.
  std::vector<std::int64_t> balance(n, 0);  // outgoing - incoming
  for (int i = 0; i < static_cast<int>(edges.size()); ++i) {
    balance[edges[i].from] += flows[i];
    balance[edges[i].to] -= flows[i];
  }
  bool conservation_ok = true;
  for (int v = 0; v < n; ++v) {
    if (v != s && v != t && balance[v] != 0) {
      conservation_ok = false;
      r.add("flow_conservation", false,
            "vertex " + std::to_string(v) + " has net flow " +
                std::to_string(balance[v]));
      break;
    }
  }
  if (conservation_ok) r.add("flow_conservation", true);

  // 3. Source net outflow equals sink net inflow, and both equal the value
  //    claimed by the solver.
  r.source_outflow = balance[s];
  r.sink_inflow = -balance[t];
  r.add("source_sink_balance", r.source_outflow == r.sink_inflow,
        "out(s)=" + std::to_string(r.source_outflow) +
            " in(t)=" + std::to_string(r.sink_inflow));
  r.add("claimed_value_matches_flow",
        claimed_flow_value == r.source_outflow &&
            r.source_outflow == r.sink_inflow && claimed_flow_value >= 0,
        "claimed " + std::to_string(claimed_flow_value) +
            " vs measured out(s)=" + std::to_string(r.source_outflow));

  // 4. Rebuild the residual network from scratch. Crucially, every input
  //    edge contributes its OWN pair of residual arcs: forward c-f and
  //    reverse f. A separate original reverse edge produces separate arcs,
  //    as required (residual reverse arcs are never shared with original
  //    edges).
  std::vector<std::vector<ResArc>> residual(n);
  for (int i = 0; i < static_cast<int>(edges.size()); ++i) {
    residual[edges[i].from].push_back(
        {edges[i].to, edges[i].capacity - flows[i]});
    residual[edges[i].to].push_back({edges[i].from, flows[i]});
  }
  std::vector<unsigned char> reachable(n, 0);
  std::queue<int> q;
  reachable[s] = 1;
  q.push(s);
  while (!q.empty()) {
    int u = q.front();
    q.pop();
    for (const ResArc& a : residual[u]) {
      if (a.residual > 0 && !reachable[a.to]) {
        reachable[a.to] = 1;
        q.push(a.to);
      }
    }
  }

  // 5. The reported partition must be exactly the residual reachable set,
  //    must contain s, and must not contain t (no augmenting path => max).
  bool partition_ok = claimed_source_side[s] == 1 &&
                      claimed_source_side[t] == 0;
  for (int v = 0; v < n && partition_ok; ++v) {
    if (claimed_source_side[v] != reachable[v]) partition_ok = false;
  }
  r.add("partition_is_residual_reachable_set", partition_ok,
        "reported S must equal the BFS-reachable set from s "
        "(s in S, t not in S)");

  // 6. Cut/flow structural conditions. For an original edge e = (u,v):
  //    u in S, v in T  => f(e) == c(e) (saturated forward cut edge),
  //    u in T, v in S  => f(e) == 0 (its reverse residual would otherwise
  //                       reach u from s).
  bool saturation_ok = true;
  for (int i = 0; i < static_cast<int>(edges.size()); ++i) {
    int u = edges[i].from;
    int v = edges[i].to;
    if (reachable[u] && !reachable[v] && flows[i] != edges[i].capacity) {
      saturation_ok = false;
      r.add("cut_edges_saturated", false,
            "edge '" + edges[i].id + "' crosses S->T with flow " +
                std::to_string(flows[i]) + " < capacity " +
                std::to_string(edges[i].capacity));
      break;
    }
    if (!reachable[u] && reachable[v] && flows[i] != 0) {
      saturation_ok = false;
      r.add("cut_edges_saturated", false,
            "edge '" + edges[i].id + "' crosses T->S with nonzero flow " +
                std::to_string(flows[i]));
      break;
    }
  }
  if (saturation_ok) r.add("cut_edges_saturated", true);

  // 7. Cut capacity. By conservation it equals the flow value whenever all
  //    S->T edges are saturated and T->S edges carry zero, but recompute it
  //    independently from capacities anyway.
  for (int i = 0; i < static_cast<int>(edges.size()); ++i) {
    if (reachable[edges[i].from] && !reachable[edges[i].to]) {
      r.cut_value += edges[i].capacity;
    }
  }
  r.add("cut_value_equals_flow_value",
        r.cut_value == claimed_flow_value &&
            r.cut_value == r.source_outflow,
        "cut=" + std::to_string(r.cut_value) +
            " flow=" + std::to_string(claimed_flow_value));

  return r;
}

}  // namespace mcut
