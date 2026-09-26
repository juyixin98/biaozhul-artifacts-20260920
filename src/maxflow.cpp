#include "maxflow.hpp"

#include <queue>

namespace mcut {

Dinic::Dinic(int num_vertices) : adj_(num_vertices) {}

void Dinic::add_original_edge(int edge_id, int from, int to,
                              std::int64_t capacity) {
  // The forward arc is an original edge; its paired reverse arc is a pure
  // residual artifact (edge_id == -1), even when a separate original edge
  // in the reverse direction exists.
  Arc forward;
  forward.to = to;
  forward.edge_id = edge_id;
  forward.residual = capacity;

  Arc backward;
  backward.to = from;
  backward.edge_id = Arc::kResidualArc;
  backward.residual = 0;

  // Resolve paired indices BEFORE inserting: for a self loop (from == to)
  // both arcs land in the same adjacency list, one after the other.
  int forward_index = static_cast<int>(adj_[from].size());
  int backward_index = (from == to)
                           ? forward_index + 1
                           : static_cast<int>(adj_[to].size());
  forward.rev = backward_index;
  backward.rev = forward_index;

  adj_[from].push_back(forward);
  adj_[to].push_back(backward);

  if (edge_id >= static_cast<int>(forward_vertex_.size())) {
    forward_vertex_.resize(edge_id + 1, -1);
    forward_index_.resize(edge_id + 1, -1);
    original_capacity_.resize(edge_id + 1, 0);
  }
  forward_vertex_[edge_id] = from;
  forward_index_[edge_id] = forward_index;
  original_capacity_[edge_id] = capacity;
}

std::int64_t Dinic::edge_flow(int edge_id) const {
  const Arc& fwd = adj_[forward_vertex_[edge_id]][forward_index_[edge_id]];
  return original_capacity_[edge_id] - fwd.residual;
}

std::int64_t Dinic::edge_capacity(int edge_id) const {
  return original_capacity_[edge_id];
}

bool Dinic::build_level_graph(int source, int sink,
                              std::vector<int>& level) const {
  std::fill(level.begin(), level.end(), -1);
  level[source] = 0;
  std::queue<int> q;
  q.push(source);
  while (!q.empty()) {
    int u = q.front();
    q.pop();
    for (const Arc& arc : adj_[u]) {
      if (arc.residual > 0 && level[arc.to] < 0) {
        level[arc.to] = level[u] + 1;
        q.push(arc.to);
      }
    }
  }
  return level[sink] >= 0;
}

namespace {

// DFS for one blocking-flow augmentation. `next_arc` implements the current
// arc optimization. Recursion depth is bounded by the number of vertices
// (<= 10 000 by kMaxVertices), which stays well within default stack limits.
std::int64_t augment(std::vector<std::vector<Arc>>& adj, int u, int sink,
                     std::int64_t pushed, const std::vector<int>& level,
                     std::vector<int>& next_arc) {
  if (u == sink || pushed == 0) return pushed;
  for (int& i = next_arc[u]; i < static_cast<int>(adj[u].size()); ++i) {
    Arc& arc = adj[u][i];
    if (arc.residual <= 0 || level[arc.to] != level[u] + 1) continue;
    std::int64_t sent = augment(adj, arc.to, sink,
                                std::min(pushed, arc.residual), level, next_arc);
    if (sent == 0) continue;
    arc.residual -= sent;
    adj[arc.to][arc.rev].residual += sent;
    return sent;
  }
  return 0;
}

}  // namespace

std::int64_t Dinic::compute_max_flow(int source, int sink) {
  std::int64_t total = 0;
  std::vector<int> level(adj_.size());
  constexpr std::int64_t kUnbounded =
      static_cast<std::int64_t>(1) << 62;

  while (build_level_graph(source, sink, level)) {
    std::vector<int> next_arc(adj_.size(), 0);
    while (true) {
      std::int64_t sent = augment(adj_, source, sink, kUnbounded, level,
                                  next_arc);
      if (sent == 0) break;
      total += sent;
    }
  }
  return total;
}

std::vector<unsigned char> Dinic::residual_reachable(int source) const {
  std::vector<unsigned char> seen(adj_.size(), 0);
  std::queue<int> q;
  seen[source] = 1;
  q.push(source);
  while (!q.empty()) {
    int u = q.front();
    q.pop();
    for (const Arc& arc : adj_[u]) {
      if (arc.residual > 0 && !seen[arc.to]) {
        seen[arc.to] = 1;
        q.push(arc.to);
      }
    }
  }
  return seen;
}

}  // namespace mcut
