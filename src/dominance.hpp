// Dominance analysis for a control-flow graph with a designated entry node.
//
// Definitions (for nodes REACHABLE from the entry):
//   * d dominates b (d dom b) iff every entry->b path passes through d.
//   * idom(b) is the unique immediate (closest) dominator of b; idom(entry)=entry.
//   * dominance frontier DF(b) = { n : there is a predecessor p of n with
//       b dominates p and b does not strictly dominate n }.
// Unreachable nodes get idom = -1, empty dom sets and empty frontiers.
//
// Three independently implemented solvers are provided:
//   1. Lengauer-Tarjan (near-linear production solver).
//   2. Naive set-iteration dataflow solver (small-scale reference).
//   3. Exhaustive simple-path enumeration (small-scale reference).
// DF is computed two independent ways (idom-tree walk and dom-set definition).
#pragma once

#include "graph.hpp"
#include <vector>

namespace dom {

struct LTResult {
    std::vector<char> reachable;            // size n
    std::vector<int> preorder;              // DFS preorder from entry (reachable only)
    std::vector<int> idom;                  // idom[v]; idom[entry]=entry; -1 unreachable
    std::vector<std::vector<int>> df;       // dominance frontier; empty for unreachable
    int reachableCount = 0;
};

// Production solver: Lengauer-Tarjan with ancestor path compression,
// followed by the Cooper-Harvey-Kennedy dominance-frontier walk.
LTResult lengauerTarjan(const Graph& g);

struct NaiveResult {
    std::vector<int> idom;                  // idom[entry]=entry; -1 unreachable
    std::vector<std::vector<int>> domSets;  // full dominator sets (sorted), empty if unreachable
    std::vector<std::vector<int>> df;       // frontier derived straight from dom sets
    int iterations = 0;                     // dataflow fixpoint rounds
};

// Reference solver #1: iterative set intersection
//   Dom(entry)={entry}; Dom(b)={b} U intersect(Dom(p) over reachable preds p)
// run in reverse postorder until fixpoint.
NaiveResult naiveIterative(const Graph& g);

struct PathEnumResult {
    std::vector<std::vector<int>> domSets;  // intersection over ALL simple entry->b paths
    long long totalPaths = 0;               // number of simple paths enumerated
};

// Reference solver #2: enumerate every simple path from the entry and intersect
// their node sets per target. Exponential; throws if totalPaths exceeds cap.
PathEnumResult pathEnumeration(const Graph& g, long long capPaths);

// Dominance queries (a,b are node indices).
// Unreachable queried nodes: everything except equal-node identity is false.
bool dominates(const LTResult& r, int a, int b);
bool properlyDominates(const LTResult& r, int a, int b);

// Comparison helpers used by the test harness.
bool sameIdom(const std::vector<int>& x, const std::vector<int>& y);
bool sameDomSets(const std::vector<std::vector<int>>& x,
                 const std::vector<std::vector<int>>& y);
bool sameFrontiers(const std::vector<std::vector<int>>& x,
                   const std::vector<std::vector<int>>& y);

} // namespace dom
