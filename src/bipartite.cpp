#include "bipartite.hpp"

#include <algorithm>
#include <queue>

namespace bipartite {

namespace {

// Collects the distinct values in `values` into sorted output plus an
// id -> dense index map.
void indexVertices(const std::vector<long long>& values,
                   std::vector<long long>& orderedIds,
                   std::unordered_map<long long, size_t>& indexMap) {
  std::vector<long long> distinct = values;
  std::sort(distinct.begin(), distinct.end());
  distinct.erase(std::unique(distinct.begin(), distinct.end()), distinct.end());
  orderedIds = std::move(distinct);
  indexMap.clear();
  indexMap.reserve(orderedIds.size() * 2 + 1);
  for (size_t i = 0; i < orderedIds.size(); ++i) {
    indexMap.emplace(orderedIds[i], i);
  }
}

}  // namespace

Matcher::Matcher(const std::vector<Edge>& edges,
                 const std::vector<long long>& declaredLeft,
                 const std::vector<long long>& declaredRight) {
  std::vector<long long> leftValues = declaredLeft;
  std::vector<long long> rightValues = declaredRight;
  leftValues.reserve(leftValues.size() + edges.size());
  rightValues.reserve(rightValues.size() + edges.size());
  for (const Edge& edge : edges) {
    leftValues.push_back(edge.left);
    rightValues.push_back(edge.right);
  }
  indexVertices(leftValues, leftIds_, leftIndex_);
  indexVertices(rightValues, rightIds_, rightIndex_);

  adjacency_.assign(leftIds_.size(), {});
  for (const Edge& edge : edges) {
    size_t li = leftIndex_.at(edge.left);
    size_t ri = rightIndex_.at(edge.right);
    adjacency_[li].push_back(ri);
  }
  for (auto& neighbors : adjacency_) {
    std::sort(neighbors.begin(), neighbors.end());
    neighbors.erase(std::unique(neighbors.begin(), neighbors.end()),
                    neighbors.end());
    edgeCount_ += neighbors.size();
  }
}

const Solution& Matcher::solve() {
  if (solved_) return solution_;

  const size_t leftSize = leftIds_.size();
  const size_t rightSize = rightIds_.size();
  matchLeft_.assign(leftSize, -1);
  matchRight_.assign(rightSize, -1);
  dist_.assign(leftSize, 0);

  while (augmentPhase()) {
    // augmentPhase() performs the DFS round itself.
  }

  buildCertificate();
  solved_ = true;
  return solution_;
}

bool Matcher::augmentPhase() {
  // Standard Hopcroft-Karp layered BFS (CLRS BFS, with NIL as a shared
  // sink for free right vertices): distNil_ is the shortest augmenting
  // distance; only edges in layers below it are used by the DFS round.
  std::queue<size_t> queue;
  for (size_t leftIndex = 0; leftIndex < leftIds_.size(); ++leftIndex) {
    if (matchLeft_[leftIndex] == -1) {
      dist_[leftIndex] = 0;
      queue.push(leftIndex);
    } else {
      dist_[leftIndex] = -1;
    }
  }
  distNil_ = -1;

  while (!queue.empty()) {
    size_t leftIndex = queue.front();
    queue.pop();
    if (distNil_ != -1 && dist_[leftIndex] >= distNil_) continue;
    for (size_t rightIndex : adjacency_[leftIndex]) {
      long long nextLeft = matchRight_[rightIndex];
      if (nextLeft == -1) {
        if (distNil_ == -1) distNil_ = dist_[leftIndex] + 1;
      } else if (dist_[static_cast<size_t>(nextLeft)] == -1) {
        dist_[static_cast<size_t>(nextLeft)] = dist_[leftIndex] + 1;
        queue.push(static_cast<size_t>(nextLeft));
      }
    }
  }

  if (distNil_ == -1) return false;

  visited_.assign(leftIds_.size(), 0);
  bool augmented = false;
  for (size_t leftIndex = 0; leftIndex < leftIds_.size(); ++leftIndex) {
    if (matchLeft_[leftIndex] == -1 && dfs(leftIndex)) {
      augmented = true;
    }
  }
  return augmented;
}

bool Matcher::dfs(size_t leftIndex) {
  if (visited_[leftIndex]) return false;
  visited_[leftIndex] = 1;
  for (size_t rightIndex : adjacency_[leftIndex]) {
    long long nextLeft = matchRight_[rightIndex];
    // A free right vertex is only accepted at the shortest augmenting
    // distance; otherwise the layered-graph invariant breaks.
    bool reachesSink =
        nextLeft == -1 ? dist_[leftIndex] + 1 == distNil_
                       : dist_[static_cast<size_t>(nextLeft)] ==
                             dist_[leftIndex] + 1;
    if (reachesSink &&
        (nextLeft == -1 || dfs(static_cast<size_t>(nextLeft)))) {
      matchLeft_[leftIndex] = static_cast<long long>(rightIndex);
      matchRight_[rightIndex] = static_cast<long long>(leftIndex);
      return true;
    }
  }
  dist_[leftIndex] = -1;  // dead end: do not retry within this phase
  return false;
}

void Matcher::buildCertificate() {
  const size_t leftSize = leftIds_.size();
  const size_t rightSize = rightIds_.size();

  solution_.matching.clear();
  for (size_t li = 0; li < leftSize; ++li) {
    if (matchLeft_[li] != -1) {
      solution_.matching.push_back(
          Edge{leftIds_[li], rightIds_[static_cast<size_t>(matchLeft_[li])]});
    }
  }
  std::sort(solution_.matching.begin(), solution_.matching.end(),
            [](const Edge& a, const Edge& b) {
              if (a.left != b.left) return a.left < b.left;
              return a.right < b.right;
            });
  solution_.matchingSize = solution_.matching.size();

  // Konig's theorem: mark vertices reachable via alternating paths from
  // unmatched left vertices (unmatched edges L->R, matched edges R->L).
  reachableLeftFlags_.assign(leftSize, 0);
  reachableRightFlags_.assign(rightSize, 0);
  std::queue<std::pair<char, size_t>> workQueue;
  for (size_t li = 0; li < leftSize; ++li) {
    if (matchLeft_[li] == -1) {
      reachableLeftFlags_[li] = 1;
      workQueue.push({'L', li});
    }
  }
  while (!workQueue.empty()) {
    auto [side, index] = workQueue.front();
    workQueue.pop();
    if (side == 'L') {
      for (size_t ri : adjacency_[index]) {
        // From a left vertex only traverse edges not in the matching.
        if (matchLeft_[index] != static_cast<long long>(ri) &&
            !reachableRightFlags_[ri]) {
          reachableRightFlags_[ri] = 1;
          workQueue.push({'R', ri});
        }
      }
    } else {
      long long matchedLeft = matchRight_[index];
      if (matchedLeft != -1) {
        size_t li = static_cast<size_t>(matchedLeft);
        if (!reachableLeftFlags_[li]) {
          reachableLeftFlags_[li] = 1;
          workQueue.push({'L', li});
        }
      }
    }
  }

  // Minimum vertex cover = (unreachable left) union (reachable right).
  solution_.minVertexCover.clear();
  for (size_t li = 0; li < leftSize; ++li) {
    if (!reachableLeftFlags_[li]) {
      solution_.minVertexCover.push_back(
          Solution::CoverVertex{'L', leftIds_[li]});
    }
  }
  for (size_t ri = 0; ri < rightSize; ++ri) {
    if (reachableRightFlags_[ri]) {
      solution_.minVertexCover.push_back(
          Solution::CoverVertex{'R', rightIds_[ri]});
    }
  }
  std::sort(solution_.minVertexCover.begin(),
            solution_.minVertexCover.end(),
            [](const Solution::CoverVertex& a,
               const Solution::CoverVertex& b) {
              if (a.side != b.side) return a.side < b.side;  // 'L' < 'R'
              return a.id < b.id;
            });
  solution_.coverSize = solution_.minVertexCover.size();

  solution_.reachableLeft.clear();
  solution_.reachableRight.clear();
  for (size_t li = 0; li < leftSize; ++li) {
    if (reachableLeftFlags_[li]) solution_.reachableLeft.push_back(leftIds_[li]);
  }
  for (size_t ri = 0; ri < rightSize; ++ri) {
    if (reachableRightFlags_[ri]) {
      solution_.reachableRight.push_back(rightIds_[ri]);
    }
  }
}

}  // namespace bipartite
