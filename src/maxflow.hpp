#pragma once

#include <cstdint>
#include <vector>

namespace mcut {

// A single arc in the residual network.
//
// Invariant that implements the requirement "residual reverse arcs are kept
// separately from original-graph reverse edges":
//   * Every original input edge e = (u -> v, c) creates exactly ONE forward
//     Arc with edge_id == e. If the input also contains an edge v -> u, that
//     is a different original edge and gets its own forward Arc. Parallel
//     original edges likewise get distinct forward Arcs.
//   * Each forward Arc is paired with a residual reverse Arc whose
//     edge_id == kResidualArc (-1). An original v -> u edge is NEVER reused
//     as the residual reverse of u -> v; the two live side by side as
//     independent entries of the adjacency list.
struct Arc {
  static constexpr int kResidualArc = -1;

  int to = -1;
  int rev = -1;                 // index of the paired arc in adj[to]
  int edge_id = kResidualArc;   // input edge index, or -1 for a residual arc
  std::int64_t residual = 0;    // residual capacity
};

// Dinic's algorithm (level graphs + blocking flows), implemented from
// scratch. Integer capacities only; no external solver is used anywhere.
class Dinic {
 public:
  explicit Dinic(int num_vertices);

  // Registers one original edge. The forward arc stores edge_id; the paired
  // reverse arc is marked kResidualArc. `capacity` may be 0.
  void add_original_edge(int edge_id, int from, int to,
                         std::int64_t capacity);

  // Computes (and returns) the maximum s-t flow. Safe to call once.
  std::int64_t compute_max_flow(int source, int sink);

  // Flow carried by an original edge = its original capacity minus the
  // remaining capacity of its forward arc.
  std::int64_t edge_flow(int edge_id) const;
  std::int64_t edge_capacity(int edge_id) const;

  // Vertices reachable from `source` through arcs with positive residual
  // capacity. After compute_max_flow this is exactly a minimum-cut source
  // side.
  std::vector<unsigned char> residual_reachable(int source) const;

  int num_vertices() const { return static_cast<int>(adj_.size()); }

 private:
  std::vector<std::vector<Arc>> adj_;
  // Location of the forward arc of each original edge.
  std::vector<int> forward_vertex_;
  std::vector<int> forward_index_;
  std::vector<std::int64_t> original_capacity_;

  bool build_level_graph(int source, int sink,
                         std::vector<int>& level) const;
};

}  // namespace mcut
