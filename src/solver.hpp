#pragma once
// Offline dynamic graph connectivity.
//
// Model: ops[0..m-1] are executed in order. A query at index i sees exactly
// the graph produced by the add/del ops with index < i. An edge added at
// index a and deleted at index d is therefore visible to queries with index
// in (a, d), i.e. the inclusive leaf range [a+1, d-1] of the time segment
// tree. An edge never deleted is visible over [a+1, m-1].
//
// Deletion matches edge *instances*: each del(u,v) removes the most recent
// unmatched add(u,v) (LIFO), so parallel edges are supported. A del with no
// active instance is an error reported with its op index.
//
// solve_offline: segment tree over time + rollback DSU (no path compression).
//   Complexity: O((E log m + m) log n), where E = number of edge instances.
// solve_reference: naive per-query BFS, O(Q * (n + E)). Used as the
//   ground-truth oracle in differential tests and via --reference.
#include <algorithm>
#include <functional>
#include <map>
#include <queue>
#include <string>
#include <utility>
#include <vector>

#include "rollback_dsu.hpp"

namespace dynconn {

constexpr long kMaxVertices = 200000;
constexpr long kMaxOps = 200000;

struct Op {
    enum class Type { Add, Del, Query };
    Type type;
    int u = 0;
    int v = 0;
};

struct SolveResult {
    bool ok = false;
    std::string error;          // set when ok == false
    long error_index = -1;      // op index that caused the error (-1 if n/a)
    std::vector<bool> answers;  // one entry per query op, in op order
};

struct Plan {
    struct Interval {
        int u, v;  // canonical: u <= v
        int l, r;  // inclusive range of op indices where the edge is visible
    };
    std::string error;
    long error_index = -1;
    std::vector<Interval> intervals;
};

// Validates the op sequence and computes edge lifetime intervals.
// Shared by both solvers so their error semantics are identical.
inline Plan build_plan(long n, const std::vector<Op>& ops) {
    Plan plan;
    if (n < 1 || n > kMaxVertices) {
        plan.error = "vertex count n=" + std::to_string(n) +
                     " out of allowed range [1, " + std::to_string(kMaxVertices) + "]";
        return plan;
    }
    if ((long)ops.size() > kMaxOps) {
        plan.error = "too many ops: " + std::to_string(ops.size()) +
                     " (max " + std::to_string(kMaxOps) + ")";
        return plan;
    }
    const long m = (long)ops.size();
    std::map<std::pair<int, int>, std::vector<long>> active;  // pair -> stack of add indices
    for (long i = 0; i < m; ++i) {
        const Op& op = ops[i];
        if (op.u < 0 || op.u >= n || op.v < 0 || op.v >= n) {
            plan.error = "vertex id out of range [0, " + std::to_string(n) + ")";
            plan.error_index = i;
            return plan;
        }
        int a = std::min(op.u, op.v);
        int b = std::max(op.u, op.v);
        if (op.type == Op::Type::Add) {
            active[{a, b}].push_back(i);
        } else if (op.type == Op::Type::Del) {
            auto it = active.find({a, b});
            if (it == active.end() || it->second.empty()) {
                plan.error = "delete of edge (" + std::to_string(a) + "," +
                             std::to_string(b) + ") with no active instance";
                plan.error_index = i;
                return plan;
            }
            long start = it->second.back();  // LIFO instance matching
            it->second.pop_back();
            plan.intervals.push_back({a, b, (int)(start + 1), (int)(i - 1)});
        }
    }
    for (const auto& kv : active) {
        for (long start : kv.second) {
            plan.intervals.push_back({kv.first.first, kv.first.second,
                                      (int)(start + 1), (int)(m - 1)});
        }
    }
    return plan;
}

inline SolveResult make_error(const Plan& plan) {
    SolveResult res;
    res.ok = false;
    res.error = plan.error;
    res.error_index = plan.error_index;
    return res;
}

// Segment tree over time + rollback DSU.
inline SolveResult solve_offline(long n, const std::vector<Op>& ops) {
    Plan plan = build_plan(n, ops);
    if (!plan.error.empty()) return make_error(plan);

    SolveResult res;
    res.ok = true;
    const int m = (int)ops.size();
    std::vector<int> qid(m, -1);
    int qcount = 0;
    for (int i = 0; i < m; ++i) {
        if (ops[i].type == Op::Type::Query) qid[i] = qcount++;
    }
    res.answers.assign(qcount, false);
    if (m == 0) return res;

    std::vector<std::vector<std::pair<int, int>>> seg(4 * (std::size_t)m);
    std::function<void(int, int, int, int, int, std::pair<int, int>)> add_range =
        [&](int node, int nl, int nr, int l, int r, std::pair<int, int> e) {
            if (l <= nl && nr <= r) {
                seg[node].push_back(e);
                return;
            }
            int mid = nl + (nr - nl) / 2;
            if (l <= mid) add_range(node * 2, nl, mid, l, r, e);
            if (r > mid) add_range(node * 2 + 1, mid + 1, nr, l, r, e);
        };
    for (const auto& iv : plan.intervals) {
        if (iv.l <= iv.r) add_range(1, 0, m - 1, iv.l, iv.r, {iv.u, iv.v});
    }

    RollbackDSU dsu((int)n);
    std::function<void(int, int, int)> dfs = [&](int node, int nl, int nr) {
        std::size_t cp = dsu.checkpoint();
        for (const auto& e : seg[node]) dsu.unite(e.first, e.second);
        if (nl == nr) {
            if (qid[nl] >= 0)
                res.answers[qid[nl]] = dsu.connected(ops[nl].u, ops[nl].v);
        } else {
            int mid = nl + (nr - nl) / 2;
            dfs(node * 2, nl, mid);
            dfs(node * 2 + 1, mid + 1, nr);
        }
        dsu.rollback(cp);
    };
    dfs(1, 0, m - 1);
    return res;
}

// Naive reference: replay ops, BFS from scratch at every query.
inline SolveResult solve_reference(long n, const std::vector<Op>& ops) {
    Plan plan = build_plan(n, ops);
    if (!plan.error.empty()) return make_error(plan);

    SolveResult res;
    res.ok = true;
    std::map<std::pair<int, int>, long> edge_count;
    std::vector<char> visited((std::size_t)n);
    std::queue<int> bfs;

    for (const Op& op : ops) {
        int a = std::min(op.u, op.v);
        int b = std::max(op.u, op.v);
        if (op.type == Op::Type::Add) {
            edge_count[{a, b}]++;
        } else if (op.type == Op::Type::Del) {
            auto it = edge_count.find({a, b});
            if (--(it->second) == 0) edge_count.erase(it);
        } else {
            std::vector<std::vector<int>> adj((std::size_t)n);
            for (const auto& kv : edge_count) {
                adj[kv.first.first].push_back(kv.first.second);
                adj[kv.first.second].push_back(kv.first.first);
            }
            std::fill(visited.begin(), visited.end(), 0);
            while (!bfs.empty()) bfs.pop();
            visited[op.u] = 1;
            bfs.push(op.u);
            while (!bfs.empty()) {
                int x = bfs.front();
                bfs.pop();
                for (int y : adj[x]) {
                    if (!visited[y]) {
                        visited[y] = 1;
                        bfs.push(y);
                    }
                }
            }
            res.answers.push_back(visited[op.v] != 0);
        }
    }
    return res;
}

} // namespace dynconn
