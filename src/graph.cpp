#include "graph.hpp"

#include <algorithm>
#include <unordered_set>

namespace domtree {

Graph Graph::build(
    const std::vector<std::string>& nodes,
    const std::vector<std::pair<std::string, std::string>>& labeled_edges,
    std::string& error) {
  Graph g;
  if (nodes.empty()) {
    error = "node list must contain at least the entry node";
    return g;
  }
  if (static_cast<int>(nodes.size()) > kMaxNodes) {
    error = "too many nodes (limit " + std::to_string(kMaxNodes) + ")";
    return g;
  }
  if (static_cast<int>(labeled_edges.size()) > kMaxEdges) {
    error = "too many edges (limit " + std::to_string(kMaxEdges) + ")";
    return g;
  }

  g.names_.reserve(nodes.size());
  for (const std::string& label : nodes) {
    if (label.empty()) {
      error = "node label must not be empty";
      return Graph();
    }
    if (static_cast<int>(label.size()) > kMaxLabelLen) {
      error = "node label exceeds " + std::to_string(kMaxLabelLen) +
              " characters: " + label.substr(0, 32) + "...";
      return Graph();
    }
    auto inserted = g.label_to_id_.emplace(label, static_cast<int>(g.names_.size()));
    if (!inserted.second) {
      error = "duplicate node label: " + label;
      return Graph();
    }
    g.names_.push_back(label);
  }

  int n = static_cast<int>(g.names_.size());
  g.succ_.assign(n, {});
  g.pred_.assign(n, {});
  g.edges_.reserve(labeled_edges.size());

  // De-duplicate parallel edges so algorithms see simple adjacency lists.
  std::unordered_set<long long> seen;
  seen.reserve(labeled_edges.size() * 2);
  for (const auto& e : labeled_edges) {
    int u = g.id(e.first);
    int v = g.id(e.second);
    if (u < 0) {
      error = "edge references unknown node: " + e.first;
      return Graph();
    }
    if (v < 0) {
      error = "edge references unknown node: " + e.second;
      return Graph();
    }
    long long key = static_cast<long long>(u) * (kMaxNodes + 1LL) + v;
    if (!seen.insert(key).second) continue;  // duplicate edge, ignore
    g.succ_[u].push_back(v);
    g.pred_[v].push_back(u);
    g.edges_.push_back({u, v});
  }
  return g;
}

bool Graph::has_edge(int u, int v) const {
  const std::vector<int>& s = succ_[u];
  return std::find(s.begin(), s.end(), v) != s.end();
}

}  // namespace domtree
