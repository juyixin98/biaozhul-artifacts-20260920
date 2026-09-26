#pragma once

#include <cstddef>
#include <string>
#include <vector>

namespace dcsolve {

// Rollback-capable disjoint set union.
//
// IMPORTANT: find() never performs path compression. Path compression would
// rewrite parent pointers that are not recorded on the undo stack, making
// rollback unsound. Union-by-size keeps tree depth O(log n) on its own, and
// every merge is recorded so it can be reversed exactly.
class RollbackDSU {
 public:
  explicit RollbackDSU(int n);

  // Plain root walk, no compression.
  int find(int x) const;

  // Merge the components of u and v by size. Returns true iff two distinct
  // components were merged (and therefore one undo record was pushed).
  bool unite(int u, int v);

  bool connected(int u, int v) const { return find(u) == find(v); }

  std::size_t checkpoint() const { return history_.size(); }
  void rollback(std::size_t checkpoint);

  std::size_t unionCalls() const { return unionCalls_; }
  std::size_t mergeCalls() const { return mergeCalls_; }

 private:
  struct MergeRecord {
    int childRoot;   // root that was attached below
    int parentRoot;  // root that kept the top slot
    int oldSize;     // size of parentRoot before the merge
  };

  std::vector<int> parent_;
  std::vector<int> size_;
  std::vector<MergeRecord> history_;
  std::size_t unionCalls_ = 0;
  std::size_t mergeCalls_ = 0;
};

struct QueryEvent {
  int t = 0;
  int u = 0;
  int v = 0;
  int opIndex = 0;
  bool connected = false;
};

struct SolveStats {
  int numVertices = 0;
  int numOps = 0;
  int numAdds = 0;
  int numDeletes = 0;
  int numQueries = 0;
  std::size_t segmentPlacements = 0;  // edge copies stored across tree nodes
  std::size_t unionCalls = 0;
  std::size_t mergeCalls = 0;
  std::size_t maxRollbackDepth = 0;
};

enum class OpKind { Add, Delete, Query };

struct OpInput {
  OpKind kind = OpKind::Query;
  int u = 0;        // add/query: first endpoint
  int v = 0;        // add/query: second endpoint
  int edgeId = -1;  // delete: the instance id returned by a prior add
  int t = 0;        // query: requested time (0-based op index convention)
};

enum class SolveErrorCode {
  None,
  VertexOutOfRange,
  UnknownEdgeId,
  DuplicateDelete,
};

struct SolveResult {
  bool ok = false;
  SolveErrorCode code = SolveErrorCode::None;
  int errorOpIndex = -1;
  std::string error;
  std::vector<QueryEvent> queries;
  SolveStats stats;
};

// Runs the offline algorithm: replay ops to derive per-instance active query
// intervals, distribute intervals over a segment tree, then DFS with a
// rollback DSU. Deletions match exact add instances by id, so parallel edges
// are independent.
SolveResult solveSegmentTree(int n, const struct OpInput* ops, std::size_t numOps);

// Naive small-scale reference: for every query, build the graph from all
// instances active at that query and run an iterative BFS. Independent code
// path from the segment-tree solver, used for cross-checking.
SolveResult solveNaiveBFS(int n, const struct OpInput* ops, std::size_t numOps);

}  // namespace dcsolve
