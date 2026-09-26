// Network model and residual graph structures.
//
// Key representation invariant (requirement: residual reverse edges and
// original reverse edges are stored separately):
//
//   * Every INPUT edge e = (u, v, c) produces its OWN pair of residual arcs:
//       - a forward arc u -> v with residual capacity c (artificial=false)
//       - an artificial residual reverse arc v -> u with capacity 0
//         (artificial=true)
//   * If the input also contains an edge (v, u), it gets a SEPARATE pair.
//     The solver never merges, deduplicates or "cancels" opposite edges, so
//     parallel edges and original reverse edges stay distinguishable all the
//     way through to the JSON evidence.
#pragma once

#include <string>
#include <vector>

namespace mincut {

struct InputEdge {
  std::string id;
  int from = -1;
  int to = -1;
  long long capacity = 0;
};

struct Network {
  std::vector<std::string> node_names;
  std::vector<InputEdge> edges;
  int source = -1;
  int sink = -1;
};

struct ResidualArc {
  int to = -1;
  int rev = -1;  // index of the paired arc in adjacency[to]
  long long cap = 0;
  int edge_id = -1;  // index into Network::edges
  bool artificial = false;  // true: solver-created residual reverse arc
};

// Location of an original edge's forward arc in the residual graph.
struct ArcLocation {
  int node = -1;
  int index = -1;
};

struct SolveStats {
  int bfs_rounds = 0;
  long long augmentations = 0;  // number of successful DFS augmentations
};

struct SolveResult {
  // Final residual graph (one adjacency vector per node, arcs in insertion
  // order). Owned by the result so it can be inspected after solving.
  std::vector<std::vector<ResidualArc>> residual;
  std::vector<ArcLocation> forward_arc;  // indexed by original edge id
  long long max_flow = 0;
  std::vector<char> source_reachable;  // nodes reachable from s with cap > 0
  SolveStats stats;
};

// Adds one forward arc plus its paired artificial reverse arc.
// Both arcs belong to original edge `edge_id`; the reverse is flagged
// artificial so it can never be confused with an input edge going the
// opposite direction.
inline void addResidualPair(std::vector<std::vector<ResidualArc>>& g, int from,
                            int to, long long capacity, int edge_id) {
  // When from == to (self-loop) both arcs land in the same adjacency list,
  // so the backward arc's index is one beyond the forward arc's.
  int forward_index = static_cast<int>(g[from].size());
  int backward_index = static_cast<int>(g[to].size()) + (from == to ? 1 : 0);
  g[from].push_back(ResidualArc{to, backward_index, capacity, edge_id, false});
  g[to].push_back(ResidualArc{from, forward_index, 0, edge_id, true});
}

}  // namespace mincut
