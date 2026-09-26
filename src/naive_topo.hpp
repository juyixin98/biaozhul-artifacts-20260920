// Naive small-scale reference: recompute the topological order from scratch
// after every edge insertion with Kahn's algorithm. Intentionally O(n + m)
// per insertion — it is the auditable baseline the incremental solver is
// differential-tested against, never used by the server's hot path.
#pragma once

#include <algorithm>
#include <vector>

#include "topo.hpp"

namespace topo {

struct NaiveInsertResult {
    bool accepted = false;
    bool duplicate = false;
    bool cycle = false;
    std::vector<int> cyclePath;
};

class NaiveTopo {
public:
    explicit NaiveTopo(int n) : g_(n), order_() {
        order_.reserve(n);
        for (int i = 0; i < n; ++i) order_.push_back(i);
    }

    const std::vector<int>& order() const { return order_; }
    bool hasEdge(int u, int v) const {
        return std::find(g_.out[u].begin(), g_.out[u].end(), v) != g_.out[u].end();
    }
    std::size_t edgeCount() const { return g_.edges; }
    const Graph& graph() const { return g_; }

    NaiveInsertResult insertEdge(int u, int v) {
        NaiveInsertResult r;
        if (u == v) {
            r.cycle = true;
            r.cyclePath = {u, u};
            return r;
        }
        if (hasEdge(u, v)) {
            r.accepted = true;
            r.duplicate = true;
            return r;
        }

        // Tentatively Kahn a *copy* that contains the candidate edge.
        // Small scale only: the copy is intentional, for auditability.
        Graph trial = g_;
        trial.addEdge(u, v); // duplicate-free; no-op if somehow present
        std::vector<int> next;
        if (!kahnOrder(trial, next)) {
            r.cycle = true;
            r.cyclePath = findCyclePath(u, v);
            return r; // original graph untouched
        }

        g_ = std::move(trial);
        order_ = std::move(next);
        r.accepted = true;
        return r;
    }

private:
    Graph g_;
    std::vector<int> order_;

    // DFS for an existing path v ->* u; witness is [u, v, ..., u].
    std::vector<int> findCyclePath(int u, int v) {
        std::vector<int> parent(g_.n, -1);
        std::vector<char> seen(g_.n, 0);
        std::vector<int> st = {v};
        seen[v] = 1;
        bool found = (v == u);
        while (!st.empty() && !found) {
            int x = st.back();
            st.pop_back();
            for (int y : g_.out[x]) {
                if (seen[y]) continue;
                seen[y] = 1;
                parent[y] = x;
                if (y == u) { found = true; break; }
                st.push_back(y);
            }
        }
        if (!found) return {u, v, u};
        std::vector<int> vToU;
        for (int x = u; x != v; x = parent[x]) vToU.push_back(x);
        vToU.push_back(v);
        std::reverse(vToU.begin(), vToU.end());
        std::vector<int> cycle = {u};
        for (int x : vToU) cycle.push_back(x);
        if (cycle.back() != u) cycle.push_back(u);
        return cycle;
    }
};

} // namespace topo
