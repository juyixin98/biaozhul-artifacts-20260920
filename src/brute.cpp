#include "brute.hpp"

#include <algorithm>
#include <functional>
#include <unordered_map>

namespace brute {

namespace {

struct DenseGraph {
  std::vector<long long> leftIds;
  std::vector<long long> rightIds;
  std::vector<std::vector<size_t>> adjacency;  // left index -> right indices
  std::vector<std::pair<size_t, size_t>> edges;
};

DenseGraph buildDense(const std::vector<bipartite::Edge>& edges,
                      const std::vector<long long>& leftVertices,
                      const std::vector<long long>& rightVertices) {
  DenseGraph graph;
  graph.leftIds = leftVertices;
  graph.rightIds = rightVertices;
  std::sort(graph.leftIds.begin(), graph.leftIds.end());
  graph.leftIds.erase(std::unique(graph.leftIds.begin(), graph.leftIds.end()),
                      graph.leftIds.end());
  std::sort(graph.rightIds.begin(), graph.rightIds.end());
  graph.rightIds.erase(
      std::unique(graph.rightIds.begin(), graph.rightIds.end()),
      graph.rightIds.end());

  std::unordered_map<long long, size_t> leftIndex;
  std::unordered_map<long long, size_t> rightIndex;
  for (size_t i = 0; i < graph.leftIds.size(); ++i) {
    leftIndex.emplace(graph.leftIds[i], i);
  }
  for (size_t i = 0; i < graph.rightIds.size(); ++i) {
    rightIndex.emplace(graph.rightIds[i], i);
  }

  graph.adjacency.assign(graph.leftIds.size(), {});
  for (const bipartite::Edge& edge : edges) {
    size_t li = leftIndex.at(edge.left);
    size_t ri = rightIndex.at(edge.right);
    graph.adjacency[li].push_back(ri);
    graph.edges.emplace_back(li, ri);
  }
  for (auto& neighbors : graph.adjacency) {
    std::sort(neighbors.begin(), neighbors.end());
    neighbors.erase(std::unique(neighbors.begin(), neighbors.end()),
                    neighbors.end());
  }
  std::sort(graph.edges.begin(), graph.edges.end());
  graph.edges.erase(std::unique(graph.edges.begin(), graph.edges.end()),
                    graph.edges.end());
  return graph;
}

}  // namespace

bool withinLimits(size_t leftCount, size_t rightCount, size_t edgeCount) {
  return leftCount + rightCount <= kMaxTotalVertices &&
         edgeCount <= kMaxEdges;
}

size_t exhaustiveMatchingSize(
    const std::vector<bipartite::Edge>& edges,
    const std::vector<long long>& leftVertices,
    const std::vector<long long>& rightVertices) {
  DenseGraph graph = buildDense(edges, leftVertices, rightVertices);
  const size_t leftSize = graph.leftIds.size();

  std::vector<char> rightUsed(graph.rightIds.size(), 0);
  size_t best = 0;
  size_t current = 0;

  // Plain depth-first enumeration: at each left vertex either leave it
  // unmatched or match it to any as-yet unused right neighbor.
  // NOLINTNEXTLINE(misc-no-recursion): bounded by kMaxTotalVertices
  std::function<void(size_t)> search = [&](size_t position) {
    if (current > best) best = current;
    if (position == leftSize) return;
    if (current + (leftSize - position) <= best) return;  // upper-bound prune
    search(position + 1);
    for (size_t ri : graph.adjacency[position]) {
      if (!rightUsed[ri]) {
        rightUsed[ri] = 1;
        ++current;
        search(position + 1);
        --current;
        rightUsed[ri] = 0;
      }
    }
  };
  search(0);
  return best;
}

ReferenceResult exhaustiveMinVertexCover(
    const std::vector<bipartite::Edge>& edges,
    const std::vector<long long>& leftVertices,
    const std::vector<long long>& rightVertices) {
  DenseGraph graph = buildDense(edges, leftVertices, rightVertices);
  const size_t leftSize = graph.leftIds.size();
  const size_t total = leftSize + graph.rightIds.size();

  ReferenceResult result;
  // Edgeless graph: the empty set covers every edge.
  result.minVertexCoverSize = 0;

  unsigned int bestMask = 0;
  size_t bestSize = total;
  // kMaxTotalVertices (20) < 32, so 1u << total never overflows.
  unsigned int limit = total == 0 ? 0u : (1u << total);
  for (unsigned int mask = 0; mask < limit; ++mask) {
    size_t size = static_cast<size_t>(__builtin_popcount(mask));
    if (size >= bestSize) continue;
    bool covers = true;
    for (const auto& [li, ri] : graph.edges) {
      bool leftChosen = (mask & (1u << li)) != 0;
      bool rightChosen = (mask & (1u << (leftSize + ri))) != 0;
      if (!leftChosen && !rightChosen) {
        covers = false;
        break;
      }
    }
    if (covers) {
      bestSize = size;
      bestMask = mask;
      if (size == 0) break;  // cannot do better
    }
  }

  result.minVertexCoverSize = bestSize;
  for (size_t li = 0; li < leftSize; ++li) {
    if (bestMask & (1u << li)) {
      result.minVertexCover.push_back(
          bipartite::Solution::CoverVertex{'L', graph.leftIds[li]});
    }
  }
  for (size_t ri = 0; ri < graph.rightIds.size(); ++ri) {
    if (bestMask & (1u << (leftSize + ri))) {
      result.minVertexCover.push_back(
          bipartite::Solution::CoverVertex{'R', graph.rightIds[ri]});
    }
  }
  return result;
}

}  // namespace brute
