// Dinic's blocking-flow algorithm for maximum flow (hand-written, no external
// solver). Non-negative integer capacities, int64 arithmetic.
#pragma once

#include <queue>
#include <vector>

#include "network.h"

namespace mincut {

class Dinic {
 public:
  explicit Dinic(const Network& net) : net_(net) {}

  SolveResult run() {
    SolveResult result;
    const int n = static_cast<int>(net_.node_names.size());
    result.residual.assign(n, {});
    result.forward_arc.resize(net_.edges.size());
    for (int i = 0; i < static_cast<int>(net_.edges.size()); ++i) {
      const InputEdge& e = net_.edges[i];
      result.forward_arc[i] = ArcLocation{
          e.from, static_cast<int>(result.residual[e.from].size())};
      addResidualPair(result.residual, e.from, e.to, e.capacity, i);
    }

    result.source_reachable.assign(n, 0);
    std::vector<int> level(n);
    std::vector<int> next_arc(n);

    while (bfs(result.residual, level, result.source_reachable)) {
      ++result.stats.bfs_rounds;
      std::fill(next_arc.begin(), next_arc.end(), 0);
      while (true) {
        long long pushed = dfs(net_.source, kInfinity, result.residual, level,
                               next_arc);
        if (pushed == 0) break;
        result.max_flow += pushed;
        ++result.stats.augmentations;
      }
    }
    return result;
  }

 private:
  static constexpr long long kInfinity = (1LL << 62);

  const Network& net_;

  // Builds a BFS level graph over arcs with residual capacity > 0.
  // Also records every node reached from the source; that set is the
  // source side S of a minimum s-t cut.
  bool bfs(const std::vector<std::vector<ResidualArc>>& g,
           std::vector<int>& level, std::vector<char>& reachable) {
    std::fill(level.begin(), level.end(), -1);
    std::fill(reachable.begin(), reachable.end(), 0);
    std::queue<int> q;
    level[net_.source] = 0;
    reachable[net_.source] = 1;
    q.push(net_.source);
    while (!q.empty()) {
      int u = q.front();
      q.pop();
      for (const ResidualArc& a : g[u]) {
        if (a.cap > 0 && level[a.to] == -1) {
          level[a.to] = level[u] + 1;
          reachable[a.to] = 1;
          q.push(a.to);
        }
      }
    }
    return level[net_.sink] != -1;
  }

  long long dfs(int u, long long pushed,
                std::vector<std::vector<ResidualArc>>& g,
                std::vector<int>& level, std::vector<int>& next_arc) {
    if (u == net_.sink || pushed == 0) return pushed;
    for (int& i = next_arc[u]; i < static_cast<int>(g[u].size()); ++i) {
      ResidualArc& a = g[u][i];
      if (a.cap <= 0 || level[a.to] != level[u] + 1) continue;
      long long d = dfs(a.to, std::min(pushed, a.cap), g, level, next_arc);
      if (d == 0) continue;
      a.cap -= d;
      g[a.to][a.rev].cap += d;
      return d;
    }
    return 0;
  }
};

}  // namespace mincut
