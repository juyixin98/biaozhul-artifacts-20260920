#pragma once

#include <cstdint>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>

namespace topo {

// Result of a single edge insertion attempt.
struct InsertResult {
    bool ok = false;          // request accepted (edge present afterwards)
    bool duplicated = false;  // edge already existed; graph unchanged
    bool cycle = false;       // edge would create a cycle; graph unchanged
    bool rejected = false;    // refused by a hard scale limit; graph unchanged
    std::string error;        // human-readable reason when rejected
    std::vector<int> cyclePath;  // vertex ids on the cycle, closed via last->first
    int64_t visited = 0;      // distinct vertices touched by the incremental search
};

struct VerifyReport {
    bool valid = false;             // maintained order respects every edge
    bool permutation = false;       // order is a permutation of all vertices
    bool acyclic = false;           // independent full Kahn scan finds no cycle
    std::vector<int> kahnOrder;     // order produced by the naive reference
};

// Incremental topological-order maintenance for a directed graph.
//
// Invariant (while the graph is nonempty): `order` is a topological ordering,
// i.e. for every edge u->v, pos[u] < pos[v]. New vertices are appended at the
// end. Inserting u->v when u already lies after v triggers a bounded
// bidirectional search over the position window; on success a contiguous
// segment is reordered by moving the "reaches-u" block left and the
// "reachable-from-v" block right. A failing (cyclic) insertion mutates nothing.
class IncrementalTopo {
public:
    // Fixed default scale limits enforced at the API boundary.
    static constexpr int kMaxNodes = 100000;
    static constexpr int64_t kMaxEdges = 1000000;

    IncrementalTopo();
    // reserveNodes pre-allocates storage; the optional caps default to the
    // fixed public limits (lower caps are mainly useful for testing).
    explicit IncrementalTopo(int reserveNodes,
                             int maxNodes = kMaxNodes,
                             int64_t maxEdges = kMaxEdges);

    int maxNodes() const { return maxNodes_; }
    int64_t maxEdges() const { return maxEdges_; }

    // Registers a vertex. Returns false if it already exists.
    bool addNode(int id);

    // Whether an external vertex id is already registered.
    bool hasNode(int id) const { return idToIndex_.count(id) != 0; }

    // Attempts to insert edge u->v. Endpoints are auto-registered.
    // Never mutates state when the result is a cycle.
    InsertResult insertEdge(int u, int v);

    // Full recomputation from scratch (naive small-scale reference, Kahn).
    // Returns false via `acyclic` in the report if a cycle exists.
    VerifyReport verify() const;

    // Returns the current topological order in external vertex ids.
    // Built on demand in O(n); the insert path never does a full scan.
    std::vector<int> order() const {
        std::vector<int> out;
        out.reserve(order_.size());
        for (int idx : order_) out.push_back(indexToId_[idx]);
        return out;
    }
    int numNodes() const { return static_cast<int>(indexToId_.size()); }
    int64_t numEdges() const { return edgeCount_; }
    int64_t totalVisited() const { return totalVisited_; }
    int64_t reorderedCount() const { return reorderedCount_; }

    // Direct adjacency access (used by tests / diagnostics).
    const std::unordered_set<int>& successors(int idx) const { return succ_[idx]; }

private:
    int internId(int id);          // resolve id -> internal index, creating if needed
    int rawInsert(int u, int v);   // returns internal u,v; -1 if over limit

    std::unordered_map<int, int> idToIndex_;
    std::vector<int> indexToId_;
    std::vector<std::unordered_set<int>> succ_;
    std::vector<std::unordered_set<int>> pred_;

    std::vector<int> order_;      // internal indices
    std::vector<int> pos_;        // vertex -> position in order_

    // Reusable search scratch space (generation stamps avoid O(n) clearing).
    std::vector<int> stampF_;
    std::vector<int> stampB_;
    std::vector<int> parentF_;
    std::vector<int> parentB_;
    int genF_ = 0;
    int genB_ = 0;

    int64_t edgeCount_ = 0;
    int64_t totalVisited_ = 0;
    int64_t reorderedCount_ = 0;
    int maxNodes_ = kMaxNodes;
    int64_t maxEdges_ = kMaxEdges;
};

}  // namespace topo
