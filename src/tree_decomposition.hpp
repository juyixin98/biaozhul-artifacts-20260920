// Tree decomposition constructed from an elimination ordering.
#pragma once

#include <cstdint>
#include <utility>
#include <vector>

#include "graph.hpp"

namespace tdw {

struct TreeDecomposition {
    // bags[i] is the vertex set of the bag created when order[i] was
    // eliminated (bags are indexed by elimination position).
    std::vector<std::vector<int>> bags;
    // Undirected edges between bag indices; form a tree.
    std::vector<std::pair<int, int>> bagTreeEdges;
    int width = 0; // max |bag| - 1 (0 for the empty graph)
};

// Builds the elimination-tree decomposition:
//   bag(i) = {order[i]} union {alive neighbors of order[i] at step i}
// parent link: bag(i) joins the bag of the earliest-eliminated later
// neighbor. Multiple roots (disconnected graph) are joined with empty
// intersections so the result stays a single tree.
TreeDecomposition buildDecomposition(const Graph& g,
                                     const std::vector<int>& order);

} // namespace tdw
