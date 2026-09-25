// Dominator solver for a control-flow graph rooted at an entry node.
//
// Definitions (for nodes reachable from entry):
//   d dominates v  iff every entry->v path passes through d.
//   idom(v)        is the unique immediate (closest strict) dominator.
//   dominance frontier DF(v) = { b | v dominates a predecessor of b but
//                                    v does not strictly dominate b }.
//
// Nodes unreachable from entry have NO dominators and NO idom; they are
// reported separately and never participate in the dominator tree.
//
// The production algorithm is self-implemented (no external solver):
//   1. iterative DFS reachability,
//   2. classic iterative data-flow set intersection for dominator sets,
//      processed in reverse post-order with bitset intersections,
//   3. idom derived as the deepest strict dominator,
//   4. dominance frontiers via the standard predecessor "runner" algorithm.
#pragma once

#include <cstdint>
#include <string>
#include <vector>

#include "graph.hpp"

namespace domtree {

struct SolverResult {
  std::vector<bool> reachable;

  // For unreachable v: idom[v] == -1 and dom_sets[v] is empty.
  std::vector<int> idom;
  std::vector<std::vector<uint64_t>> dom_sets;
  std::vector<std::vector<int>> frontier;
  std::vector<std::vector<int>> tree_children;
  std::vector<int> tree_depth;

  // Edges u -> v where v dominates u (natural-loop back edges); both
  // endpoints are necessarily reachable.
  std::vector<Edge> back_edges;
  // Edges with at least one unreachable endpoint, reported as evidence.
  std::vector<Edge> unreachable_edges;

  int reachable_count = 0;
  int iterations = 0;  // data-flow fixed-point rounds used

  bool reachable_node(int v) const {
    return v >= 0 && v < static_cast<int>(reachable.size()) && reachable[v];
  }

  bool dominates(int a, int b) const;
};

// Solves for the given graph and entry id. Returns false with `error`
// when the entry label does not exist.
bool solve(const Graph& g, int entry, SolverResult& out, std::string& error);

// Returns labels of the dominator tree subtree rooted at `root`
// (including root), in pre-order.
std::vector<int> dominated_subtree(const SolverResult& r, int root);

// Returns the chain root, idom(root), ... up to entry (inclusive),
// i.e. the strict dominator chain with the queried node first.
std::vector<int> dominator_chain(const SolverResult& r, int node);

}  // namespace domtree
