#include "dom.hpp"

#include <algorithm>
#include <functional>

namespace domtree {
namespace {

using Bits = std::vector<uint64_t>;

int words_for(int n) { return (n + 63) / 64; }

void set_bit(Bits& b, int i) { b[i >> 6] |= (1ULL << (i & 63)); }
bool test_bit(const Bits& b, int i) {
  return (b[i >> 6] >> (i & 63)) & 1ULL;
}

// Iterative DFS from entry; fills `reachable` and returns nodes in
// reverse post-order (RPO), which speeds up data-flow convergence.
std::vector<int> reachability_rpo(const Graph& g, int entry,
                                  std::vector<bool>& reachable) {
  int n = g.node_count();
  std::vector<int> postorder;
  postorder.reserve(n);
  std::vector<char> state(n, 0);  // 0=unseen 1=on stack 2=done
  std::vector<std::pair<int, size_t>> stack;
  stack.push_back({entry, 0});
  state[entry] = 1;
  while (!stack.empty()) {
    int v = stack.back().first;
    size_t& i = stack.back().second;
    const std::vector<int>& succ = g.successors(v);
    if (i < succ.size()) {
      int w = succ[i++];
      if (state[w] == 0) {
        state[w] = 1;
        stack.push_back({w, 0});
      }
    } else {
      state[v] = 2;
      postorder.push_back(v);
      stack.pop_back();
    }
  }
  for (int v = 0; v < n; ++v) reachable[v] = (state[v] == 2);
  // postorder -> reverse postorder
  std::reverse(postorder.begin(), postorder.end());
  return postorder;
}

// Classic iterative data-flow: Dom(entry) = {entry},
// Dom(v) = {v} ∪ ⋂ Dom(p) over reachable predecessors p.
// Returns the number of rounds used.
int compute_dom_sets(const Graph& g, int entry,
                     const std::vector<int>& rpo,
                     const std::vector<bool>& reachable,
                     std::vector<Bits>& dom) {
  int n = g.node_count();
  int words = words_for(n);
  dom.assign(n, Bits(words, 0));
  for (int v : rpo) {
    if (v == entry) continue;
    // Initialize to all-ones so intersections only shrink.
    for (int w = 0; w < words; ++w) dom[v][w] = ~0ULL;
  }
  set_bit(dom[entry], entry);

  Bits tmp(words);
  int rounds = 0;
  bool changed = true;
  while (changed) {
    changed = false;
    ++rounds;
    for (int v : rpo) {
      if (v == entry) continue;
      // tmp = intersection of Dom(p) for reachable predecessors.
      bool first = true;
      for (int p : g.predecessors(v)) {
        if (!reachable[p]) continue;
        if (first) {
          tmp = dom[p];
          first = false;
        } else {
          for (int w = 0; w < words; ++w) tmp[w] &= dom[p][w];
        }
      }
      set_bit(tmp, v);
      if (tmp != dom[v]) {
        dom[v] = tmp;
        changed = true;
      }
    }
  }
  return rounds;
}

// idom(v) is the strict dominator of v with the largest dominator set
// (the deepest one on the dominator chain).
void compute_idom(const std::vector<Bits>& dom,
                  const std::vector<int>& rpo, int entry,
                  std::vector<int>& idom) {
  int n = static_cast<int>(dom.size());
  int words = words_for(n);
  idom.assign(n, -1);
  std::vector<int> sizes(n, 0);
  for (int v : rpo) {
    int s = 0;
    for (int w = 0; w < words; ++w) s += __builtin_popcountll(dom[v][w]);
    sizes[v] = s;
  }
  for (int v : rpo) {
    if (v == entry) continue;
    int best = -1;
    for (int d = 0; d < n; ++d) {
      if (d == v || !test_bit(dom[v], d)) continue;
      if (best == -1 || sizes[d] > sizes[best]) best = d;
    }
    idom[v] = best;
  }
}

// Dominance frontiers via the standard "runner" algorithm:
// for each block b with reachable predecessors, walk each reachable
// predecessor p up the idom chain until idom(b), marking DF[runner] += b.
// Blocks with a single distinct reachable predecessor contribute nothing
// (the while condition is immediately false), except a pure entry self
// loop, which the formal definition puts in its own frontier; processing
// every block rather than only join blocks keeps both cases identical.
// A single stamp vector replaces per-block marker arrays (O(n) memory).
void compute_frontiers(const Graph& g, const std::vector<int>& idom,
                       const std::vector<bool>& reachable,
                       std::vector<std::vector<int>>& frontier) {
  int n = g.node_count();
  frontier.assign(n, {});
  std::vector<int> mark(n, 0);
  int stamp = 0;
  for (int b = 0; b < n; ++b) {
    if (!reachable[b]) continue;
    ++stamp;  // one stamp per block: b is added to any DF at most once
    for (int p : g.predecessors(b)) {
      if (!reachable[p]) continue;
      int runner = p;
      while (runner != -1 && runner != idom[b]) {
        if (mark[runner] != stamp) {
          mark[runner] = stamp;
          frontier[runner].push_back(b);
        }
        runner = idom[runner];
      }
    }
  }
  for (auto& f : frontier) std::sort(f.begin(), f.end());
}

void compute_tree(const std::vector<int>& idom,
                  const std::vector<bool>& reachable,
                  std::vector<std::vector<int>>& children,
                  std::vector<int>& depth) {
  int n = static_cast<int>(idom.size());
  children.assign(n, {});
  depth.assign(n, -1);
  for (int v = 0; v < n; ++v) {
    if (reachable[v] && idom[v] != -1) children[idom[v]].push_back(v);
  }
  for (auto& c : children) std::sort(c.begin(), c.end());
  // Depths via iterative walk from each node up the chain (chains are
  // short in practice; memoize through the depth array).
  for (int v = 0; v < n; ++v) {
    if (!reachable[v]) continue;
    int d = 0;
    int cur = v;
    std::vector<int> path;
    while (cur != -1 && depth[cur] == -1) {
      path.push_back(cur);
      cur = idom[cur];
    }
    d = (cur == -1) ? 0 : depth[cur] + 1;
    for (auto it = path.rbegin(); it != path.rend(); ++it) {
      depth[*it] = d++;
    }
  }
}

}  // namespace

bool SolverResult::dominates(int a, int b) const {
  if (!reachable_node(a) || !reachable_node(b)) return false;
  return test_bit(dom_sets[b], a);
}

bool solve(const Graph& g, int entry, SolverResult& out, std::string& error) {
  int n = g.node_count();
  if (entry < 0 || entry >= n) {
    error = "entry node does not exist in the graph";
    return false;
  }
  out = SolverResult();
  out.reachable.assign(n, false);
  std::vector<int> rpo = reachability_rpo(g, entry, out.reachable);
  out.reachable_count = static_cast<int>(rpo.size());
  out.iterations =
      compute_dom_sets(g, entry, rpo, out.reachable, out.dom_sets);
  compute_idom(out.dom_sets, rpo, entry, out.idom);
  compute_frontiers(g, out.idom, out.reachable, out.frontier);
  compute_tree(out.idom, out.reachable, out.tree_children, out.tree_depth);

  for (const Edge& e : g.edges()) {
    bool ru = out.reachable[e.from], rv = out.reachable[e.to];
    if (ru && rv) {
      if (out.dominates(e.to, e.from)) out.back_edges.push_back(e);
    } else {
      out.unreachable_edges.push_back(e);
    }
  }
  return true;
}

std::vector<int> dominated_subtree(const SolverResult& r, int root) {
  std::vector<int> out;
  if (!r.reachable_node(root)) return out;
  std::vector<int> stack = {root};
  while (!stack.empty()) {
    int v = stack.back();
    stack.pop_back();
    out.push_back(v);
    const auto& kids = r.tree_children[v];
    for (auto it = kids.rbegin(); it != kids.rend(); ++it) stack.push_back(*it);
  }
  return out;
}

std::vector<int> dominator_chain(const SolverResult& r, int node) {
  std::vector<int> chain;
  if (!r.reachable_node(node)) return chain;
  for (int cur = node; cur != -1; cur = r.idom[cur]) chain.push_back(cur);
  return chain;
}

}  // namespace domtree
