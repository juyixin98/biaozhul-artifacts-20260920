// Incremental topological ordering maintenance for a directed graph.
//
// Edge u -> v means "u must come before v". The maintained total order is a
// permutation of all vertices such that every edge satisfies
// position[u] < position[v].
//
// Search: Pearce & Kelly (2006) bounded bidirectional search ("PK").
// On inserting u -> v:
//   * trivial accept when u precedes v in the order;
//   * otherwise run a forward search downstream of v and a backward search
//     upstream of u, both confined to the position window [v, u],
//     alternately expanding the smaller frontier;
//   * reject (cycle) if the two searches meet — nothing is changed;
//   * otherwise locally reorder the smaller visited region within the
//     window and commit the edge.
// Reference:
//   David J. Pearce, Paul H. Kelly, "A Dynamic Topological Sort Algorithm
//   for Directed Acyclic Graphs", ACM J. Exp. Algorithmics 11 (2006).
//
// Order representation: PK's local reorder is a stable partition of the
// position window ("move the visited vertices to one end"). Two naive
// layouts are costly:
//   * array  — reorder touches the whole (often huge) window, not just the
//     few visited vertices;
//   * search tree — window membership becomes an O(log n) rank query per
//     examined edge.
// We store the order as a doubly linked list annotated with 64-bit gap
// labels (the classic order-maintenance idea). Order comparison is a single
// integer compare, and a reorder unlinks the |delta| visited vertices and
// splices them as one block at the window edge in O(|delta| log |delta|)
// (the log factor is a stable sort by current label). Labels are spaced
// kLabelStride apart; a block that does not fit into a local gap triggers a
// full even renumbering, which is amortized cheap at this scale.
//
// A from-scratch Kahn implementation is provided as an independent
// reference used by the test/demo harness; it is never called by the
// incremental solver.
#pragma once

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <queue>
#include <string>
#include <unordered_set>
#include <vector>

namespace topo {

// Result of an insertion attempt. On rejection the graph is left untouched.
struct InsertResult {
    bool accepted = false;
    bool duplicate = false;         // edge already existed (accepted, no work)
    bool cycle = false;             // rejected: edge would create a cycle
    std::vector<int> cyclePath;     // closed path when cycle == true
    long long visited = 0;          // distinct vertices actually visited
    bool reordered = false;         // whether the stored order changed
};

class IncrementalTopo {
public:
    explicit IncrementalTopo(int n)
        : n_(n), out_(n), in_(n), prev_(n, -1), next_(n, -1), label_(n, 0),
          markF_(n, 0), markB_(n, 0) {
        for (int i = 0; i < n; ++i) {
            prev_[i] = i - 1;
            next_[i] = (i + 1 < n) ? i + 1 : -1;
            label_[i] = static_cast<int64_t>(i + 1) * kLabelStride;
        }
        head_ = (n > 0) ? 0 : -1;
    }

    int numVertices() const { return n_; }
    bool hasEdge(int u, int v) const { return out_[u].count(v) != 0; }
    std::size_t edgeCount() const { return edges_; }

    // Bumped on every committed structural change; rejected insertions and
    // duplicate inserts leave it untouched.
    long long revision() const { return revision_; }

    // O(n) rank lookup; the algorithm never needs exact ranks, only order
    // comparisons. Provided for diagnostics/tests.
    int positionOf(int v) const {
        int rank = 0;
        for (int x = head_; x != v; x = next_[x]) ++rank;
        return rank;
    }

    // Materialize the current order as a vertex vector.
    std::vector<int> order() const {
        std::vector<int> result;
        result.reserve(n_);
        for (int x = head_; x >= 0; x = next_[x]) result.push_back(x);
        return result;
    }

    // Attempt to insert edge u -> v.
    // A rejected insertion (cycle, including u == v) changes nothing.
    InsertResult insertEdge(int u, int v) {
        InsertResult r;
        if (u == v) {
            r.cycle = true;
            r.cyclePath = {u, u};
            return r;
        }
        if (out_[u].count(v)) {
            r.accepted = true;
            r.duplicate = true;
            return r;
        }
        if (label_[u] < label_[v]) {
            commitEdge(u, v);
            r.accepted = true;
            return r;
        }

        // Window spans [v, u] in the current order. Boundary tests are O(1).
        const int64_t lo = label_[v];
        const int64_t hi = label_[u];

        // Reusable timestamp-marked scratch space avoids per-insertion heap
        // allocation and clearing in the common case.
        ++stamp_;
        stackF_.clear();
        stackB_.clear();
        deltaF_.clear();
        deltaB_.clear();
        stackF_.push_back(v);
        stackB_.push_back(u);
        markF_[v] = stamp_;
        markB_[u] = stamp_;
        deltaF_.push_back(v);
        deltaB_.push_back(u);
        r.visited = 2;

        bool cycle = false;
        while (!cycle) {
            bool expandF = !stackF_.empty() &&
                           (stackB_.empty() || deltaF_.size() <= deltaB_.size());
            bool expandB = !expandF && !stackB_.empty();
            if (!expandF && !expandB) break;

            if (expandF) {
                int x = stackF_.back();
                stackF_.pop_back();
                for (int y : out_[x]) {
                    if (label_[y] > hi) continue; // outside window: stays above
                    if (markF_[y] != stamp_) {
                        markF_[y] = stamp_;
                        deltaF_.push_back(y);
                        ++r.visited;
                        if (markB_[y] == stamp_) { cycle = true; break; }
                        stackF_.push_back(y);
                    }
                }
            } else {
                int x = stackB_.back();
                stackB_.pop_back();
                for (int y : in_[x]) {
                    if (label_[y] < lo) continue; // outside window: stays below
                    if (markB_[y] != stamp_) {
                        markB_[y] = stamp_;
                        deltaB_.push_back(y);
                        ++r.visited;
                        if (markF_[y] == stamp_) { cycle = true; break; }
                        stackB_.push_back(y);
                    }
                }
            }
        }

        if (cycle) {
            r.cyclePath = extractCycle(u, v, hi);
            r.cycle = true;
            return r; // graph intentionally unchanged
        }

        if (deltaF_.size() <= deltaB_.size()) {
            // Downstream set (incl. v) moves to the high window edge,
            // immediately after u (u is a stayed vertex).
            moveBlockAfter(deltaF_, u);
        } else {
            // Upstream set (incl. u) moves to the low window edge,
            // immediately before v (v is a stayed vertex).
            moveBlockBefore(deltaB_, v);
        }
        r.reordered = true;
        commitEdge(u, v);
        r.accepted = true;
        return r;
    }

private:
    static constexpr int64_t kLabelStride = 1LL << 20;
    static constexpr int64_t kLabelMax = (1LL << 62);

    int n_;
    int head_;
    std::size_t edges_ = 0;
    long long revision_ = 0;
    std::vector<std::unordered_set<int>> out_;
    std::vector<std::unordered_set<int>> in_;

    // Order as a labelled doubly linked list; label gives O(1) compares.
    std::vector<int> prev_;
    std::vector<int> next_;
    std::vector<int64_t> label_;

    // Timestamp-marked search scratch (marks start at 0; stamp_ > 0).
    long long stamp_ = 0;
    std::vector<long long> markF_;
    std::vector<long long> markB_;
    std::vector<int> stackF_, stackB_, deltaF_, deltaB_;

    void listRemove(int x) {
        int p = prev_[x];
        int q = next_[x];
        if (p >= 0) next_[p] = q; else head_ = q;
        if (q >= 0) prev_[q] = p;
        prev_[x] = next_[x] = -1;
    }

    void listLinkBetween(int x, int p, int q) {
        prev_[x] = p;
        next_[x] = q;
        if (p >= 0) next_[p] = x; else head_ = x;
        if (q >= 0) prev_[q] = x;
    }

    // Evenly renumber the whole list so adjacent gaps are `stride`.
    void renumberAll(int64_t stride) {
        int64_t lab = stride;
        for (int x = head_; x >= 0; x = next_[x]) {
            label_[x] = lab;
            lab += stride;
        }
    }

    // Assign strictly increasing labels to the d ordered block vertices,
    // all strictly between labels of anchors p and q (anchor id -1 means
    // list edge). Triggers a global renumbering if the local gap is full.
    void labelBlockBetween(const std::vector<int>& block, int p, int q) {
        const int64_t d = static_cast<int64_t>(block.size());
        const int64_t low = (p >= 0) ? label_[p] : 0;
        const int64_t high = (q >= 0) ? label_[q] : kLabelMax;
        int64_t gap = high - low - 1;

        if (gap < d) {
            // Rebuild with gaps large enough for this block anywhere.
            int64_t stride = std::max<int64_t>(kLabelStride, 2 * (d + 1));
            renumberAll(stride);
            return labelBlockBetween(block, p, q); // anchors carry new labels
        }
        int64_t step = gap / (d + 1); // >= 1, keeps every label inside gap
        for (int64_t i = 0; i < d; ++i)
            label_[block[static_cast<size_t>(i)]] = low + (i + 1) * step;
    }

    std::vector<int> sortedByOrder(const std::vector<int>& moved) {
        std::vector<int> ordered = moved;
        std::sort(ordered.begin(), ordered.end(),
                  [&](int a, int b) { return label_[a] < label_[b]; });
        return ordered;
    }

    // Unlink moved vertices and splice them (relative order preserved)
    // immediately after anchor.
    void moveBlockAfter(const std::vector<int>& moved, int anchor) {
        std::vector<int> block = sortedByOrder(moved);
        for (int x : block) listRemove(x);
        int q = next_[anchor]; // window-exterior vertex (or list tail)
        labelBlockBetween(block, anchor, q);
        int p = anchor;
        for (int x : block) {
            listLinkBetween(x, p, q);
            p = x;
        }
    }

    // Unlink moved vertices and splice them (relative order preserved)
    // immediately before anchor.
    void moveBlockBefore(const std::vector<int>& moved, int anchor) {
        std::vector<int> block = sortedByOrder(moved);
        for (int x : block) listRemove(x);
        int p = prev_[anchor]; // window-exterior vertex (or list head)
        labelBlockBetween(block, p, anchor);
        int q = anchor;
        for (auto it = block.rbegin(); it != block.rend(); ++it) {
            listLinkBetween(*it, p, q);
            q = *it;
        }
    }

    void commitEdge(int u, int v) {
        out_[u].insert(v);
        in_[v].insert(u);
        ++edges_;
        ++revision_;
    }

    // Once the frontiers meet, a path v ->* u exists in the committed graph
    // (wholly within the window). BFS from v along outgoing edges to recover
    // it, then close it with the edge under insertion: u, v, ..., u.
    std::vector<int> extractCycle(int u, int v, int64_t hi) {
        std::vector<int> par(n_, -1);
        std::vector<int> q;
        q.push_back(v);
        par[v] = v;
        size_t head = 0;
        while (head < q.size() && par[u] == -1) {
            int x = q[head++];
            for (int y : out_[x]) {
                if (par[y] != -1 || label_[y] > hi) continue;
                par[y] = x;
                q.push_back(y);
            }
        }
        std::vector<int> vToU; // reconstruct v -> ... -> u
        for (int x = u; x != v; x = par[x]) vToU.push_back(x);
        vToU.push_back(v);
        std::reverse(vToU.begin(), vToU.end());
        std::vector<int> cycle = {u};
        for (int x : vToU) cycle.push_back(x);
        if (cycle.back() != u) cycle.push_back(u);
        return cycle;
    }
};

// ---------------------------------------------------------------------------
// Independent reference: full recomputation with Kahn's algorithm.
// Returns false (orderOut incomplete) iff the graph is cyclic.
// ---------------------------------------------------------------------------
struct Graph {
    int n;
    std::vector<std::vector<int>> out;
    std::vector<std::vector<int>> in;
    std::size_t edges = 0;

    explicit Graph(int n_) : n(n_), out(n_), in(n_) {}

    void addEdge(int u, int v) {
        // Adjacency kept duplicate-free for accurate indegree accounting.
        if (std::find(out[u].begin(), out[u].end(), v) == out[u].end()) {
            out[u].push_back(v);
            in[v].push_back(u);
            ++edges;
        }
    }
};

inline bool kahnOrder(const Graph& g, std::vector<int>& orderOut) {
    std::vector<int> indeg(g.n, 0);
    for (int u = 0; u < g.n; ++u)
        for (int v : g.out[u]) ++indeg[v];

    std::queue<int> q;
    for (int i = 0; i < g.n; ++i)
        if (indeg[i] == 0) q.push(i);

    orderOut.clear();
    while (!q.empty()) {
        int u = q.front();
        q.pop();
        orderOut.push_back(u);
        for (int v : g.out[u]) {
            if (--indeg[v] == 0) q.push(v);
        }
    }
    return static_cast<int>(orderOut.size()) == g.n;
}

// Validate that `order` is a topological order of `g`: a permutation of
// [0,n) with every edge pointing forward. Empty error string means valid.
inline std::string validateOrder(const Graph& g, const std::vector<int>& order) {
    if (static_cast<int>(order.size()) != g.n)
        return "order length " + std::to_string(order.size()) +
               " != vertex count " + std::to_string(g.n);
    std::vector<int> pos(g.n, -1);
    for (int i = 0; i < g.n; ++i) {
        int v = order[i];
        if (v < 0 || v >= g.n) return "vertex id out of range: " + std::to_string(v);
        if (pos[v] != -1) return "duplicate vertex in order: " + std::to_string(v);
        pos[v] = i;
    }
    for (int u = 0; u < g.n; ++u)
        for (int v : g.out[u])
            if (pos[u] >= pos[v])
                return "edge " + std::to_string(u) + "->" + std::to_string(v) +
                       " violated (positions " + std::to_string(pos[u]) + " >= " +
                       std::to_string(pos[v]) + ")";
    return "";
}

} // namespace topo
