// Bipartite maximum matching (Hopcroft-Karp) and minimum vertex cover
// derived from the maximum matching via Konig's theorem.
//
// The graph is described by edges (u, v) where u is a left-side id and
// v is a right-side id. Ids are arbitrary integers; internally the code
// compresses them to dense indices and maps every reported result back
// to the original ids. Duplicate edges are tolerated.
#pragma once

#include <string>
#include <unordered_map>
#include <vector>

namespace bipartite {

struct Edge {
  long long left;
  long long right;
};

struct Solution {
  // Maximum matching as original-id pairs; sorted by (left, right).
  std::vector<Edge> matching;
  size_t matchingSize = 0;

  // Minimum vertex cover as vertices with a side tag; sorted by (side, id).
  struct CoverVertex {
    char side;  // 'L' or 'R'
    long long id;
  };
  std::vector<CoverVertex> minVertexCover;
  size_t coverSize = 0;

  // Reachable set of the alternating BFS/DFS search started from all
  // unmatched left vertices. Exposed for certificate explanations/tests.
  std::vector<long long> reachableLeft;
  std::vector<long long> reachableRight;
};

class Matcher {
 public:
  // Edges must already be validated; duplicate edges are de-duplicated.
  Matcher(const std::vector<Edge>& edges,
          const std::vector<long long>& declaredLeft,
          const std::vector<long long>& declaredRight);

  const Solution& solve();

  size_t leftCount() const { return leftIds_.size(); }
  size_t rightCount() const { return rightIds_.size(); }
  size_t edgeCount() const { return edgeCount_; }

 private:
  bool augmentPhase();
  bool dfs(size_t leftIndex);
  void buildCertificate();

  // Dense-index adjacency (sorted, unique).
  std::vector<std::vector<size_t>> adjacency_;
  size_t edgeCount_ = 0;

  std::vector<long long> leftIds_;
  std::vector<long long> rightIds_;
  std::unordered_map<long long, size_t> leftIndex_;
  std::unordered_map<long long, size_t> rightIndex_;

  std::vector<long long> matchLeft_;   // left index -> matched right index, or -1
  std::vector<long long> matchRight_;  // right index -> matched left index, or -1
  std::vector<long long> dist_;
  long long distNil_ = 0;  // BFS distance to the nearest free right vertex
  std::vector<char> visited_;          // per-phase DFS guard on left indices
  std::vector<char> reachableLeftFlags_;
  std::vector<char> reachableRightFlags_;
  bool solved_ = false;
  Solution solution_;
};

}  // namespace bipartite
