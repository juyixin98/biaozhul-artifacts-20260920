#include "topo.hpp"

#include <algorithm>
#include <queue>

namespace topo {

IncrementalTopo::IncrementalTopo() = default;

IncrementalTopo::IncrementalTopo(int reserveNodes, int maxNodes, int64_t maxEdges)
    : maxNodes_(maxNodes), maxEdges_(maxEdges) {
    if (reserveNodes > 0) {
        idToIndex_.reserve(reserveNodes);
        indexToId_.reserve(reserveNodes);
        succ_.reserve(reserveNodes);
        pred_.reserve(reserveNodes);
        order_.reserve(reserveNodes);
        pos_.reserve(reserveNodes);
        stampF_.reserve(reserveNodes);
        stampB_.reserve(reserveNodes);
        parentF_.reserve(reserveNodes);
        parentB_.reserve(reserveNodes);
    }
}

bool IncrementalTopo::addNode(int id) {
    auto it = idToIndex_.find(id);
    if (it != idToIndex_.end()) return false;
    int idx = static_cast<int>(indexToId_.size());
    idToIndex_.emplace(id, idx);
    indexToId_.push_back(id);
    succ_.emplace_back();
    pred_.emplace_back();
    pos_.push_back(static_cast<int>(order_.size()));
    order_.push_back(idx);
    stampF_.push_back(0);
    stampB_.push_back(0);
    parentF_.push_back(-1);
    parentB_.push_back(-1);
    return true;
}

int IncrementalTopo::internId(int id) {
    auto it = idToIndex_.find(id);
    if (it != idToIndex_.end()) return it->second;
    int idx = static_cast<int>(indexToId_.size());
    idToIndex_.emplace(id, idx);
    indexToId_.push_back(id);
    succ_.emplace_back();
    pred_.emplace_back();
    pos_.push_back(static_cast<int>(order_.size()));
    order_.push_back(idx);
    stampF_.push_back(0);
    stampB_.push_back(0);
    parentF_.push_back(-1);
    parentB_.push_back(-1);
    return idx;
}

InsertResult IncrementalTopo::insertEdge(int uId, int vId) {
    InsertResult res;

    // Self-loop is a cycle of length 1.
    if (uId == vId) {
        res.cycle = true;
        res.cyclePath = {uId};
        res.visited = 0;
        return res;
    }

    // A duplicate edge adds nothing, so it stays idempotent even when the
    // graph is already at a scale limit.
    auto itU = idToIndex_.find(uId);
    auto itV = idToIndex_.find(vId);
    if (itU != idToIndex_.end() && itV != idToIndex_.end() &&
        succ_[itU->second].count(itV->second)) {
        res.ok = true;
        res.duplicated = true;
        res.visited = 0;
        return res;
    }

    // Node cap is enforced before any vertex is created, so a rejected request
    // mutates nothing.
    int newVertices = (itU == idToIndex_.end() ? 1 : 0) +
                      (itV == idToIndex_.end() ? 1 : 0);
    if (numNodes() + newVertices > maxNodes_) {
        res.rejected = true;
        res.error = "node limit exceeded";
        return res;
    }

    // A brand-new endpoint cannot lie on any cycle, so when an endpoint is new
    // the only remaining refusal is the edge cap, and it is safe (and required
    // for zero-mutation rejection) to check it before interning the vertex.
    bool hasNewEndpoint = newVertices > 0;
    if (hasNewEndpoint && edgeCount_ + 1 > maxEdges_) {
        res.rejected = true;
        res.error = "edge limit exceeded";
        return res;
    }

    int u = internId(uId);
    int v = internId(vId);

    const int pu = pos_[u];
    const int pv = pos_[v];

    // Already correctly ordered: no cycle is possible (a path v ->* u would
    // contradict pos[u] < pos[v]); only the edge cap remains.
    if (pu < pv) {
        if (edgeCount_ + 1 > maxEdges_) {
            res.rejected = true;
            res.error = "edge limit exceeded";
            return res;
        }
        succ_[u].insert(v);
        pred_[v].insert(u);
        ++edgeCount_;
        res.ok = true;
        res.visited = 0;
        return res;
    }

    // pu > pv (self-loop handled above). Bounded bidirectional search over the
    // position window [pv, pu]:
    //   F = { x : v reaches x, pv <= pos[x] <= pu }  (forward from v)
    //   R = { x : x reaches u, pv <= pos[x] <= pu }  (backward from u)
    const int lo = pv;
    const int hi = pu;
    ++genF_;
    ++genB_;

    std::vector<int> fStack = {v};
    std::vector<int> bStack = {u};
    stampF_[v] = genF_;
    stampB_[u] = genB_;
    parentF_[v] = -1;
    parentB_[u] = -1;

    std::vector<int> fOrder;  // visitation order, used as L set
    std::vector<int> bOrder;  // visitation order, used as R set
    fOrder.push_back(v);
    bOrder.push_back(u);

    while (!fStack.empty()) {
        int x = fStack.back();
        fStack.pop_back();
        for (int y : succ_[x]) {
            int py = pos_[y];
            if (py < lo || py > hi) continue;
            if (stampF_[y] == genF_) continue;
            stampF_[y] = genF_;
            parentF_[y] = x;
            fStack.push_back(y);
            fOrder.push_back(y);
        }
    }
    while (!bStack.empty()) {
        int x = bStack.back();
        bStack.pop_back();
        for (int y : pred_[x]) {
            int py = pos_[y];
            if (py < lo || py > hi) continue;
            if (stampB_[y] == genB_) continue;
            stampB_[y] = genB_;
            parentB_[y] = x;
            bStack.push_back(y);
            bOrder.push_back(y);
        }
    }

    int64_t touched = static_cast<int64_t>(fOrder.size()) +
                      static_cast<int64_t>(bOrder.size());
    res.visited = touched;
    totalVisited_ += touched;

    // Cycle iff F and R intersect: v ->* x ->* u, closed by the new edge u->v.
    int meet = -1;
    for (int x : fOrder) {
        if (stampB_[x] == genB_) { meet = x; break; }
    }
    if (meet != -1) {
        // Existing path v ->* meet ->* u that the rejected edge u->v would
        // close. Every consecutive pair below is a real stored edge.
        std::vector<int> chain;  // v ... meet (internal indices)
        for (int x = meet; x != -1; x = parentF_[x]) chain.push_back(x);
        std::reverse(chain.begin(), chain.end());  // [v, ..., meet]
        for (int idx : chain) res.cyclePath.push_back(indexToId_[idx]);
        for (int x = parentB_[meet]; x != -1; x = parentB_[x]) {
            res.cyclePath.push_back(indexToId_[x]);  // (meet ... u]
        }
        res.cycle = true;
        return res;  // graph intentionally untouched
    }

    // Edge cap: both endpoints already existed here (a new endpoint is
    // pre-checked), and the edge is known acyclic; refuse before mutating.
    if (edgeCount_ + 1 > maxEdges_) {
        res.rejected = true;
        res.error = "edge limit exceeded";
        return res;
    }

    // No cycle: reorder the window. F (reachable from v) moves to the largest
    // positions, R (reaching u) to the smallest positions, each preserving
    // relative order; unaffected vertices keep the middle positions.
    std::vector<int> positions;
    positions.reserve(fOrder.size() + bOrder.size());
    for (int x : fOrder) positions.push_back(pos_[x]);
    for (int x : bOrder) positions.push_back(pos_[x]);
    std::sort(positions.begin(), positions.end());

    std::vector<int> rSorted = bOrder;
    std::vector<int> fSorted = fOrder;
    std::sort(rSorted.begin(), rSorted.end(),
              [&](int a, int b) { return pos_[a] < pos_[b]; });
    std::sort(fSorted.begin(), fSorted.end(),
              [&](int a, int b) { return pos_[a] < pos_[b]; });

    const size_t rN = rSorted.size();
    for (size_t i = 0; i < rN; ++i) {
        int x = rSorted[i];
        int p = positions[i];
        order_[p] = x;
        pos_[x] = p;
    }
    for (size_t i = 0; i < fSorted.size(); ++i) {
        int x = fSorted[i];
        int p = positions[rN + i];
        order_[p] = x;
        pos_[x] = p;
    }

    succ_[u].insert(v);
    pred_[v].insert(u);
    ++edgeCount_;
    ++reorderedCount_;


    res.ok = true;
    return res;
}

VerifyReport IncrementalTopo::verify() const {
    VerifyReport rep;
    const int n = numNodes();

    // 1. order_ is a permutation of 0..n-1.
    std::vector<int> seen(n, 0);
    rep.permutation = static_cast<int>(order_.size()) == n;
    if (rep.permutation) {
        for (int x : order_) {
            if (x < 0 || x >= n || seen[x]) { rep.permutation = false; break; }
            seen[x] = 1;
        }
    }

    // 2. Maintained order respects every stored edge.
    rep.valid = rep.permutation;
    if (rep.valid) {
        for (int x = 0; x < n; ++x) {
            for (int y : succ_[x]) {
                if (pos_[x] >= pos_[y]) { rep.valid = false; break; }
            }
            if (!rep.valid) break;
        }
    }

    // 3. Independent full recomputation from scratch (naive Kahn reference).
    std::vector<int> indeg(n, 0);
    for (int x = 0; x < n; ++x) {
        for (int y : succ_[x]) ++indeg[y];
    }
    std::queue<int> q;
    for (int x = 0; x < n; ++x) {
        if (indeg[x] == 0) q.push(x);
    }
    std::vector<int> kahn;
    while (!q.empty()) {
        int x = q.front();
        q.pop();
        kahn.push_back(x);
        for (int y : succ_[x]) {
            if (--indeg[y] == 0) q.push(y);
        }
    }
    rep.acyclic = static_cast<int>(kahn.size()) == n;
    rep.kahnOrder.reserve(n);
    for (int idx : kahn) rep.kahnOrder.push_back(indexToId_[idx]);

    return rep;
}

}  // namespace topo
