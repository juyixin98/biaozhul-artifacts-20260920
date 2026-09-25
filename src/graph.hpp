// Directed control-flow graph model with string node labels.
// Nodes and edges are validated and de-duplicated on construction.
#pragma once

#include <string>
#include <unordered_map>
#include <vector>

namespace domtree {

// Hard scale limits for the production solver. The naive path-enumeration
// reference uses much tighter limits of its own (see naive.hpp).
constexpr int kMaxNodes = 10000;
constexpr int kMaxEdges = 50000;
constexpr int kMaxLabelLen = 256;

struct Edge {
  int from;
  int to;
};

class Graph {
 public:
  Graph() = default;

  // Builds a graph from labeled nodes and label-pair edges.
  // On validation failure returns false and sets `error`.
  static Graph build(const std::vector<std::string>& nodes,
                     const std::vector<std::pair<std::string, std::string>>&
                         labeled_edges,
                     std::string& error);

  int node_count() const { return static_cast<int>(names_.size()); }
  int edge_count() const { return static_cast<int>(edges_.size()); }

  const std::vector<int>& successors(int v) const { return succ_[v]; }
  const std::vector<int>& predecessors(int v) const { return pred_[v]; }
  const std::string& name(int v) const { return names_[v]; }

  // Returns -1 when the label is unknown.
  int id(const std::string& label) const {
    auto it = label_to_id_.find(label);
    return it == label_to_id_.end() ? -1 : it->second;
  }

  bool has_edge(int u, int v) const;
  const std::vector<Edge>& edges() const { return edges_; }

 private:
  std::vector<std::string> names_;
  std::unordered_map<std::string, int> label_to_id_;
  std::vector<std::vector<int>> succ_;
  std::vector<std::vector<int>> pred_;
  std::vector<Edge> edges_;
};

}  // namespace domtree
