#include "mincut.hpp"

namespace mcut {

CutCertificate build_min_cut(const Problem& problem, const Dinic& dinic,
                             int source) {
  CutCertificate cert;
  cert.source_side = dinic.residual_reachable(source);
  cert.cut_value = 0;
  for (int i = 0; i < static_cast<int>(problem.edges.size()); ++i) {
    const InputEdge& e = problem.edges[i];
    if (cert.source_side[e.from] && !cert.source_side[e.to]) {
      CutEdgeInfo info;
      info.edge_id = i;
      info.capacity = e.capacity;
      info.flow = dinic.edge_flow(i);
      cert.cut_value += e.capacity;
      cert.cut_edges.push_back(info);
    }
  }
  return cert;
}

}  // namespace mcut
