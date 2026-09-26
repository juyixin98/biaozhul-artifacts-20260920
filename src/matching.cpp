#include "matching.hpp"

#include <algorithm>
#include <cstdint>
#include <deque>
#include <functional>
#include <stdexcept>

namespace {

constexpr int kUnmatched = -1;

// Hopcroft-Karp: O(E * sqrt(V)) maximum bipartite matching.
void hopcroftKarp(int nLeft, int nRight,
                  const std::vector<std::vector<int>>& adj,
                  std::vector<int>& matchLeft, std::vector<int>& matchRight) {
  matchLeft.assign(nLeft, kUnmatched);
  matchRight.assign(nRight, kUnmatched);
  std::vector<int> dist(nLeft);

  auto bfs = [&]() -> bool {
    std::deque<int> queue;
    for (int u = 0; u < nLeft; ++u) {
      if (matchLeft[u] == kUnmatched) {
        dist[u] = 0;
        queue.push_back(u);
      } else {
        dist[u] = kUnmatched;
      }
    }
    bool foundAugmentingPath = false;
    while (!queue.empty()) {
      int u = queue.front();
      queue.pop_front();
      for (int v : adj[u]) {
        int next = matchRight[v];
        if (next == kUnmatched) {
          foundAugmentingPath = true;
        } else if (dist[next] == kUnmatched) {
          dist[next] = dist[u] + 1;
          queue.push_back(next);
        }
      }
    }
    return foundAugmentingPath;
  };

  std::function<bool(int)> dfs = [&](int u) -> bool {
    for (int v : adj[u]) {
      int next = matchRight[v];
      if (next == kUnmatched ||
          (dist[next] == dist[u] + 1 && dfs(next))) {
        matchLeft[u] = v;
        matchRight[v] = u;
        return true;
      }
    }
    dist[u] = kUnmatched;
    return false;
  };

  while (bfs()) {
    for (int u = 0; u < nLeft; ++u) {
      if (matchLeft[u] == kUnmatched) {
        dfs(u);
      }
    }
  }
}

// Kőnig's theorem: from a maximum matching, the minimum vertex cover is
//   (L \ Z_L) ∪ (R ∩ Z_R)
// where Z is the set of vertices reachable from unmatched left vertices via
// alternating paths (non-matching edges L→R, matching edges R→L).
void computeMinVertexCover(const std::vector<std::vector<int>>& adj,
                           const std::vector<int>& matchLeft,
                           const std::vector<int>& matchRight,
                           std::vector<bool>& coverLeft,
                           std::vector<bool>& coverRight) {
  int nLeft = static_cast<int>(matchLeft.size());
  int nRight = static_cast<int>(matchRight.size());
  std::vector<bool> seenLeft(nLeft, false), seenRight(nRight, false);

  std::deque<int> queue;
  for (int u = 0; u < nLeft; ++u) {
    if (matchLeft[u] == kUnmatched) {
      seenLeft[u] = true;
      queue.push_back(u);
    }
  }
  // Alternating BFS. State encoding: left vertices are 0..nLeft-1, right
  // vertices are nLeft..nLeft+nRight-1.
  while (!queue.empty()) {
    int node = queue.front();
    queue.pop_front();
    if (node < nLeft) {
      int u = node;
      for (int v : adj[u]) {
        // Traverse only non-matching edges from left to right.
        if (matchLeft[u] != v && !seenRight[v]) {
          seenRight[v] = true;
          queue.push_back(nLeft + v);
        }
      }
    } else {
      int v = node - nLeft;
      // Traverse only matching edges from right to left.
      int u = matchRight[v];
      if (u != kUnmatched && !seenLeft[u]) {
        seenLeft[u] = true;
        queue.push_back(u);
      }
    }
  }

  coverLeft.assign(nLeft, false);
  coverRight.assign(nRight, false);
  for (int u = 0; u < nLeft; ++u) coverLeft[u] = !seenLeft[u];
  for (int v = 0; v < nRight; ++v) coverRight[v] = seenRight[v];
}

}  // namespace

MatchingResult bipartiteMaxMatching(int nLeft, int nRight,
                                    const std::vector<std::vector<int>>& adj) {
  if (nLeft < 0 || nRight < 0 ||
      static_cast<int>(adj.size()) != nLeft) {
    throw std::invalid_argument("bipartiteMaxMatching: inconsistent sizes");
  }
  MatchingResult result;
  result.nLeft = nLeft;
  result.nRight = nRight;
  hopcroftKarp(nLeft, nRight, adj, result.matchLeft, result.matchRight);
  for (int u = 0; u < nLeft; ++u) {
    if (result.matchLeft[u] != kUnmatched) ++result.matchingSize;
  }
  computeMinVertexCover(adj, result.matchLeft, result.matchRight,
                        result.coverLeft, result.coverRight);
  for (bool b : result.coverLeft) result.coverSize += b ? 1 : 0;
  for (bool b : result.coverRight) result.coverSize += b ? 1 : 0;
  return result;
}

int naiveMaxMatchingSize(int nLeft, int nRight,
                         const std::vector<std::vector<int>>& adj) {
  if (nRight > 24) {
    throw std::invalid_argument(
        "naiveMaxMatchingSize: nRight too large for exhaustive reference");
  }
  // dp[mask] = max matches using some prefix of left vertices; transition on
  // left vertex u: dp'[mask | (1<<v)] = max(dp'[...], dp[mask] + 1).
  const uint32_t full = (nRight >= 32) ? 0xFFFFFFFFu : ((1u << nRight) - 1u);
  std::vector<int> dp(size_t{1} << nRight, -1);
  dp[0] = 0;
  for (int u = 0; u < nLeft; ++u) {
    std::vector<int> next = dp;
    for (uint32_t mask = 0; mask <= full; ++mask) {
      if (dp[mask] < 0) continue;
      for (int v : adj[u]) {
        uint32_t bit = 1u << v;
        if (mask & bit) continue;
        uint32_t nmask = mask | bit;
        next[nmask] = std::max(next[nmask], dp[mask] + 1);
      }
    }
    dp.swap(next);
  }
  int best = 0;
  for (int value : dp) best = std::max(best, value);
  return best;
}
