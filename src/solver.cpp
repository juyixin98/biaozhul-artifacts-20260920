#include "solver.h"

#include <algorithm>
#include <queue>
#include <string>
#include <vector>

namespace dcsolve {

RollbackDSU::RollbackDSU(int n)
    : parent_(static_cast<size_t>(n)), size_(static_cast<size_t>(n), 1) {
  for (int i = 0; i < n; ++i) parent_[static_cast<size_t>(i)] = i;
}

int RollbackDSU::find(int x) const {
  // Deliberately no path compression: the only parent/self-link changes are
  // the ones recorded by unite(), so rollback can restore exact prior state.
  while (parent_[static_cast<size_t>(x)] != x) {
    x = parent_[static_cast<size_t>(x)];
  }
  return x;
}

bool RollbackDSU::unite(int u, int v) {
  ++unionCalls_;
  int ru = find(u);
  int rv = find(v);
  if (ru == rv) return false;
  if (size_[static_cast<size_t>(ru)] < size_[static_cast<size_t>(rv)]) std::swap(ru, rv);
  history_.push_back(MergeRecord{rv, ru, size_[static_cast<size_t>(ru)]});
  parent_[static_cast<size_t>(rv)] = ru;
  size_[static_cast<size_t>(ru)] += size_[static_cast<size_t>(rv)];
  ++mergeCalls_;
  return true;
}

void RollbackDSU::rollback(std::size_t checkpoint) {
  while (history_.size() > checkpoint) {
    const MergeRecord rec = history_.back();
    history_.pop_back();
    parent_[static_cast<size_t>(rec.childRoot)] = rec.childRoot;
    size_[static_cast<size_t>(rec.parentRoot)] = rec.oldSize;
  }
}

namespace {

struct InternalEdge {
  int u;
  int v;
};

// Validates the op stream and derives each add-instance's active query-index
// interval [loQuery, hiQuery). Instance ids are assigned in add order.
struct ReplayOutput {
  std::vector<InternalEdge> edges;
  std::vector<int> loQuery;
  std::vector<int> hiQuery;
  std::vector<QueryEvent> queries;
  int numAdds = 0;
  int numDeletes = 0;
};

bool validateAndReplay(int n, const OpInput* ops, size_t numOps, ReplayOutput& out,
                       std::string& error, SolveErrorCode& code, int& errorOpIndex) {
  std::vector<char> instanceActive;
  for (size_t i = 0; i < numOps; ++i) {
    const OpInput& op = ops[i];
    auto validVertex = [&](int x) { return x >= 0 && x < n; };
    switch (op.kind) {
      case OpKind::Add: {
        if (!validVertex(op.u) || !validVertex(op.v)) {
          error = "op[" + std::to_string(i) + "]: add references vertex outside [0, n)";
          code = SolveErrorCode::VertexOutOfRange;
          errorOpIndex = static_cast<int>(i);
          return false;
        }
        out.edges.push_back({op.u, op.v});
        out.loQuery.push_back(static_cast<int>(out.queries.size()));
        out.hiQuery.push_back(-1);
        instanceActive.push_back(1);
        ++out.numAdds;
        break;
      }
      case OpKind::Delete: {
        if (op.edgeId < 0 || op.edgeId >= static_cast<int>(out.edges.size())) {
          error = "op[" + std::to_string(i) + "]: delete references unknown edge_id " +
                  std::to_string(op.edgeId) + " (no add ever returned it)";
          code = SolveErrorCode::UnknownEdgeId;
          errorOpIndex = static_cast<int>(i);
          return false;
        }
        const size_t id = static_cast<size_t>(op.edgeId);
        if (!instanceActive[id]) {
          error = "op[" + std::to_string(i) + "]: duplicate delete of edge_id " +
                  std::to_string(op.edgeId) + ": instance was already deleted";
          code = SolveErrorCode::DuplicateDelete;
          errorOpIndex = static_cast<int>(i);
          return false;
        }
        instanceActive[id] = 0;
        out.hiQuery[id] = static_cast<int>(out.queries.size());
        ++out.numDeletes;
        break;
      }
      case OpKind::Query: {
        if (!validVertex(op.u) || !validVertex(op.v)) {
          error = "op[" + std::to_string(i) + "]: query references vertex outside [0, n)";
          code = SolveErrorCode::VertexOutOfRange;
          errorOpIndex = static_cast<int>(i);
          return false;
        }
        QueryEvent q;
        q.t = op.t;
        q.u = op.u;
        q.v = op.v;
        q.opIndex = static_cast<int>(i);
        out.queries.push_back(q);
        break;
      }
    }
  }
  // Instances never deleted stay active through the final query.
  for (size_t id = 0; id < out.edges.size(); ++id) {
    if (out.hiQuery[id] == -1) out.hiQuery[id] = static_cast<int>(out.queries.size());
  }
  return true;
}

// Segment tree over the Q query positions. Every edge interval is decomposed
// into O(log Q) canonical nodes that store the edge.
class SegmentTree {
 public:
  explicit SegmentTree(int numLeaves)
      : q_(numLeaves), nodes_(static_cast<size_t>(4) * static_cast<size_t>(std::max(1, numLeaves))) {}

  void addInterval(int lo, int hi, const InternalEdge& edge) {
    if (lo >= hi) return;  // lifetime never overlaps any query position
    addRec(1, 0, q_, lo, hi, edge);
  }

  const std::vector<InternalEdge>& nodeAt(size_t node) const { return nodes_[node]; }
  size_t slotCount() const { return nodes_.size(); }

  // Traverses nodes in leaf order; the visitor applies/rolls back via the DSU
  // it exposes and answers each leaf query.
  template <class Visitor>
  void dfs(Visitor& visitor) const {
    if (q_ == 0) return;
    dfsRec(1, 0, q_, visitor);
  }

 private:
  void addRec(int node, int l, int r, int lo, int hi, const InternalEdge& edge) {
    if (lo >= r || hi <= l) return;
    if (lo <= l && r <= hi) {
      nodes_[static_cast<size_t>(node)].push_back(edge);
      return;
    }
    const int mid = l + (r - l) / 2;
    addRec(node * 2, l, mid, lo, hi, edge);
    addRec(node * 2 + 1, mid, r, lo, hi, edge);
  }

  template <class Visitor>
  void dfsRec(int node, int l, int r, Visitor& visitor) const {
    RollbackDSU& dsu = visitor.dsu();
    const size_t checkpoint = dsu.checkpoint();
    for (const InternalEdge& e : nodes_[static_cast<size_t>(node)]) dsu.unite(e.u, e.v);
    visitor.observeStack(dsu.checkpoint());
    if (r - l == 1) {
      visitor.leaf(l, dsu);
    } else {
      const int mid = l + (r - l) / 2;
      dfsRec(node * 2, l, mid, visitor);
      dfsRec(node * 2 + 1, mid, r, visitor);
    }
    dsu.rollback(checkpoint);
  }

  int q_;
  std::vector<std::vector<InternalEdge>> nodes_;
};

struct DfsVisitor {
  RollbackDSU dsu_;
  std::vector<QueryEvent>& queries;
  size_t maxStack = 0;

  DfsVisitor(int n, std::vector<QueryEvent>& qs) : dsu_(n), queries(qs) {}

  RollbackDSU& dsu() { return dsu_; }
  void observeStack(size_t depth) { maxStack = std::max(maxStack, depth); }

  void leaf(int pos, const RollbackDSU& d) {
    QueryEvent& q = queries[static_cast<size_t>(pos)];
    q.connected = d.connected(q.u, q.v);
  }
};

SolveResult finalize(int n, size_t numOps, const ReplayOutput& replay, SolveStats stats,
                     std::vector<QueryEvent> queries) {
  SolveResult result;
  result.ok = true;
  result.queries = std::move(queries);
  stats.numVertices = n;
  stats.numOps = static_cast<int>(numOps);
  stats.numAdds = replay.numAdds;
  stats.numDeletes = replay.numDeletes;
  stats.numQueries = static_cast<int>(replay.queries.size());
  result.stats = stats;
  return result;
}

}  // namespace

SolveResult solveSegmentTree(int n, const OpInput* ops, size_t numOps) {
  SolveResult result;
  ReplayOutput replay;
  std::string error;
  SolveErrorCode code = SolveErrorCode::None;
  int errorOpIndex = -1;
  if (!validateAndReplay(n, ops, numOps, replay, error, code, errorOpIndex)) {
    result.error = std::move(error);
    result.code = code;
    result.errorOpIndex = errorOpIndex;
    return result;
  }

  const int q = static_cast<int>(replay.queries.size());
  SegmentTree tree(q);
  for (size_t id = 0; id < replay.edges.size(); ++id) {
    tree.addInterval(replay.loQuery[id], replay.hiQuery[id], replay.edges[id]);
  }

  // Evidence of O(E log Q) storage: total edge copies over all tree nodes.
  size_t placements = 0;
  for (size_t node = 1; node < tree.slotCount(); ++node) {
    placements += tree.nodeAt(node).size();
  }

  DfsVisitor visitor(n, replay.queries);
  tree.dfs(visitor);

  SolveStats stats;
  stats.segmentPlacements = placements;
  stats.unionCalls = visitor.dsu_.unionCalls();
  stats.mergeCalls = visitor.dsu_.mergeCalls();
  stats.maxRollbackDepth = visitor.maxStack;
  return finalize(n, numOps, replay, stats, replay.queries);
}

SolveResult solveNaiveBFS(int n, const OpInput* ops, size_t numOps) {
  SolveResult result;
  ReplayOutput replay;
  std::string error;
  SolveErrorCode code = SolveErrorCode::None;
  int errorOpIndex = -1;
  if (!validateAndReplay(n, ops, numOps, replay, error, code, errorOpIndex)) {
    result.error = std::move(error);
    result.code = code;
    result.errorOpIndex = errorOpIndex;
    return result;
  }

  // Independent replay keeping the set of active instances; adjacency lists
  // are rebuilt from scratch for every query, then a plain BFS runs.
  std::vector<InternalEdge> allEdges;
  std::vector<char> active;
  std::vector<QueryEvent> answers;
  answers.reserve(replay.queries.size());

  std::vector<std::vector<int>> adj(static_cast<size_t>(n));
  std::vector<int> visited(static_cast<size_t>(n), -1);
  int stamp = 0;
  int adds = 0;
  int deletes = 0;

  for (size_t i = 0; i < numOps; ++i) {
    const OpInput& op = ops[i];
    if (op.kind == OpKind::Add) {
      allEdges.push_back({op.u, op.v});
      active.push_back(1);
      ++adds;
    } else if (op.kind == OpKind::Delete) {
      active[static_cast<size_t>(op.edgeId)] = 0;
      ++deletes;
    } else {
      for (auto& row : adj) row.clear();
      for (size_t id = 0; id < allEdges.size(); ++id) {
        if (active[id]) {
          adj[static_cast<size_t>(allEdges[id].u)].push_back(allEdges[id].v);
          adj[static_cast<size_t>(allEdges[id].v)].push_back(allEdges[id].u);
        }
      }
      ++stamp;
      std::queue<int> bfs;
      bfs.push(op.u);
      visited[static_cast<size_t>(op.u)] = stamp;
      while (!bfs.empty()) {
        const int x = bfs.front();
        bfs.pop();
        for (int y : adj[static_cast<size_t>(x)]) {
          if (visited[static_cast<size_t>(y)] != stamp) {
            visited[static_cast<size_t>(y)] = stamp;
            bfs.push(y);
          }
        }
      }
      QueryEvent qe;
      qe.t = op.t;
      qe.u = op.u;
      qe.v = op.v;
      qe.opIndex = static_cast<int>(i);
      qe.connected = visited[static_cast<size_t>(op.v)] == stamp;
      answers.push_back(qe);
    }
  }

  SolveStats stats;
  stats.numAdds = adds;
  stats.numDeletes = deletes;
  return finalize(n, numOps, replay, stats, std::move(answers));
}

}  // namespace dcsolve
