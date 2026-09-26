#pragma once

#include <cstdint>
#include <vector>

#include "graph.hpp"
#include "maxflow.hpp"

namespace mcut {

struct CutEdgeInfo {
  int edge_id = -1;
  std::int64_t capacity = 0;
  std::int64_t flow = 0;
};

struct CutCertificate {
  // source_side[v] == 1 iff v is reachable from s in the residual network.
  std::vector<unsigned char> source_side;
  std::vector<CutEdgeInfo> cut_edges;  // original edges directed S -> T
  std::int64_t cut_value = 0;
};

// Builds the minimum-cut certificate from a solved Dinic instance by a
// residual-graph BFS. Reads edge flows through Dinic's per-edge accessors.
CutCertificate build_min_cut(const Problem& problem, const Dinic& dinic,
                             int source);

}  // namespace mcut
