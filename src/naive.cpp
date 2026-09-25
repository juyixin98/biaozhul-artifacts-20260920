#include "naive.hpp"

#include <algorithm>
#include <cstdint>
#include <queue>

namespace domtree {
namespace {

// Per-target DFS enumerating simple entry->target paths and intersecting
// their node sets. `path_mask` holds nodes on the current path.
void enumerate_paths(const Graph& g, int entry, int target,
                     uint64_t path_mask, std::vector<char>& on_path,
                     uint64_t& intersection, bool& first,
                     long long& budget, long long& total_paths,
                     std::string& error, bool& aborted) {
  if (aborted) return;
  path_mask |= (1ULL << entry);
  on_path[entry] = 1;
  if (entry == target) {
    if (first) {
      intersection = path_mask;
      first = false;
    } else {
      intersection &= path_mask;
    }
    ++total_paths;
    if (--budget <= 0) {
      error = "naive reference exceeded path budget of " +
              std::to_string(kNaiveMaxPaths);
      aborted = true;
    }
    on_path[entry] = 0;
    return;
  }
  for (int w : g.successors(entry)) {
    if (on_path[w]) continue;  // keep paths simple
    enumerate_paths(g, w, target, path_mask, on_path, intersection, first,
                    budget, total_paths, error, aborted);
    if (aborted) {
      on_path[entry] = 0;
      return;
    }
  }
  on_path[entry] = 0;
}

}  // namespace

bool naive_solve(const Graph& g, int entry, NaiveResult& out,
                 std::string& error) {
  int n = g.node_count();
  if (n > kNaiveMaxNodes) {
    error = "naive reference supports at most " +
            std::to_string(kNaiveMaxNodes) + " nodes, got " +
            std::to_string(n);
    return false;
  }
  if (entry < 0 || entry >= n) {
    error = "entry node does not exist in the graph";
    return false;
  }

  // Independent BFS reachability (deliberately different code path).
  out.reachable.assign(n, false);
  std::queue<int> q;
  out.reachable[entry] = true;
  q.push(entry);
  while (!q.empty()) {
    int v = q.front();
    q.pop();
    for (int w : g.successors(v)) {
      if (!out.reachable[w]) {
        out.reachable[w] = true;
        q.push(w);
      }
    }
  }

  std::vector<uint64_t> dom(n, 0);
  long long budget = kNaiveMaxPaths;
  out.paths_enumerated = 0;
  std::vector<char> on_path(n, 0);

  for (int target = 0; target < n; ++target) {
    if (!out.reachable[target]) continue;
    if (target == entry) {
      dom[entry] = (1ULL << entry);
      continue;
    }
    uint64_t intersection = 0;
    bool first = true;
    bool aborted = false;
    std::string err;
    enumerate_paths(g, entry, target, 0, on_path, intersection, first, budget,
                    out.paths_enumerated, err, aborted);
    if (aborted) {
      error = err;
      return false;
    }
    dom[target] = intersection;
  }

  // idom: strict dominator with the largest dominator set.
  out.idom.assign(n, -1);
  for (int v = 0; v < n; ++v) {
    if (!out.reachable[v] || v == entry) continue;
    int best = -1;
    int best_size = -1;
    for (int d = 0; d < n; ++d) {
      if (d == v || !((dom[v] >> d) & 1ULL)) continue;
      int size = __builtin_popcountll(dom[d]);
      if (size > best_size) {
        best_size = size;
        best = d;
      }
    }
    out.idom[v] = best;
  }

  // Dominance frontier straight from the definition:
  // b in DF(v) iff v dominates some predecessor of b and v does not
  // strictly dominate b.
  out.frontier.assign(n, {});
  for (int b = 0; b < n; ++b) {
    if (!out.reachable[b]) continue;
    for (int p : g.predecessors(b)) {
      if (!out.reachable[p]) continue;
      for (int v = 0; v < n; ++v) {
        if (!out.reachable[v]) continue;
        bool dominates_pred = (dom[p] >> v) & 1ULL;
        bool strictly_dominates_b =
            v != b && ((dom[b] >> v) & 1ULL);
        if (dominates_pred && !strictly_dominates_b) {
          out.frontier[v].push_back(b);
        }
      }
    }
  }
  for (auto& f : out.frontier) {
    std::sort(f.begin(), f.end());
    f.erase(std::unique(f.begin(), f.end()), f.end());
  }
  return true;
}

std::vector<std::string> compare_results(const Graph& g,
                                         const SolverResult& prod,
                                         const NaiveResult& ref) {
  int n = g.node_count();
  std::vector<std::string> mismatches;
  for (int v = 0; v < n; ++v) {
    if (prod.reachable[v] != ref.reachable[v]) {
      mismatches.push_back("reachability mismatch at node " + g.name(v));
      continue;
    }
    if (!prod.reachable[v]) {
      if (prod.idom[v] != -1) {
        mismatches.push_back("unreachable node " + g.name(v) +
                             " unexpectedly has an idom");
      }
      continue;
    }
    if (prod.idom[v] != ref.idom[v]) {
      std::string a = prod.idom[v] == -1 ? "-" : g.name(prod.idom[v]);
      std::string b = ref.idom[v] == -1 ? "-" : g.name(ref.idom[v]);
      mismatches.push_back("idom mismatch at " + g.name(v) + ": solver=" + a +
                           " naive=" + b);
    }
    if (prod.frontier[v] != ref.frontier[v]) {
      std::string got;
      for (int x : prod.frontier[v]) {
        if (!got.empty()) got += ",";
        got += g.name(x);
      }
      std::string want;
      for (int x : ref.frontier[v]) {
        if (!want.empty()) want += ",";
        want += g.name(x);
      }
      mismatches.push_back("frontier mismatch at " + g.name(v) +
                           ": solver={" + got + "} naive={" + want + "}");
    }
  }
  return mismatches;
}

}  // namespace domtree
