#include "dag.hpp"

#include <algorithm>
#include <queue>
#include <set>

namespace {

void checkNodeId(int64_t id, int64_t numNodes) {
    if (id < 0 || id >= numNodes) {
        throw DomainError("INVALID_GRAPH",
                          "node id " + std::to_string(id) + " out of range [0," +
                              std::to_string(numNodes) + ")");
    }
}

}  // namespace

Dag::Dag(int64_t numNodes, const std::vector<std::pair<int64_t, int64_t>>& edges)
    : numNodes_(numNodes) {
    if (numNodes < 1 || numNodes > kMaxNodes) {
        throw DomainError("LIMIT_EXCEEDED",
                          "num_nodes must be in [1," + std::to_string(kMaxNodes) + "], got " +
                              std::to_string(numNodes));
    }
    if (static_cast<int64_t>(edges.size()) > kMaxEdges) {
        throw DomainError("LIMIT_EXCEEDED",
                          "edge count exceeds limit " + std::to_string(kMaxEdges));
    }

    std::set<std::pair<int64_t, int64_t>> seen;
    adj_.assign(static_cast<size_t>(numNodes), {});
    std::vector<int64_t> indeg(static_cast<size_t>(numNodes), 0);
    for (const auto& [u, v] : edges) {
        checkNodeId(u, numNodes);
        checkNodeId(v, numNodes);
        if (u == v) {
            throw DomainError("INVALID_GRAPH",
                              "self-loop at node " + std::to_string(u) +
                                  " is not allowed in a DAG");
        }
        if (!seen.insert({u, v}).second) {
            throw DomainError("DUPLICATE_EDGE",
                              "edge (" + std::to_string(u) + "," + std::to_string(v) +
                                  ") listed more than once");
        }
        adj_[static_cast<size_t>(u)].push_back(v);
        ++indeg[static_cast<size_t>(v)];
    }
    for (auto& succ : adj_) std::sort(succ.begin(), succ.end());

    // Kahn's algorithm: a DAG must yield exactly numNodes popped nodes.
    std::queue<int64_t> ready;
    for (int64_t v = 0; v < numNodes; ++v) {
        if (indeg[static_cast<size_t>(v)] == 0) ready.push(v);
    }
    while (!ready.empty()) {
        int64_t u = ready.front();
        ready.pop();
        topo_.push_back(u);
        for (int64_t v : adj_[static_cast<size_t>(u)]) {
            if (--indeg[static_cast<size_t>(v)] == 0) ready.push(v);
        }
    }
    if (static_cast<int64_t>(topo_.size()) != numNodes) {
        throw DomainError("CYCLE", "graph contains a directed cycle");
    }
}

void Dag::checkEndpoint(int64_t node) const {
    if (node < 0 || node >= numNodes_) {
        throw DomainError("INVALID_REQUEST",
                          "endpoint " + std::to_string(node) + " out of range [0," +
                              std::to_string(numNodes_) + ")");
    }
}

std::vector<BigUint> Dag::countsTo(int64_t target) const {
    // Reverse topological accumulation: every edge u->v has v later in the
    // Kahn order, so when u is processed all successor counts are final.
    // count[target] = 1; count[u] = sum of count[v] over successors v.
    // Nodes that cannot reach target keep count 0.
    std::vector<BigUint> count(static_cast<size_t>(numNodes_));
    count[static_cast<size_t>(target)] = BigUint(1);
    for (auto it = topo_.rbegin(); it != topo_.rend(); ++it) {
        int64_t u = *it;
        if (u == target) continue;
        BigUint total;
        for (int64_t v : adj_[static_cast<size_t>(u)]) {
            total += count[static_cast<size_t>(v)];
        }
        count[static_cast<size_t>(u)] = std::move(total);
    }
    return count;
}

BigUint Dag::countPaths(int64_t source, int64_t target) const {
    checkEndpoint(source);
    checkEndpoint(target);
    return countsTo(target)[static_cast<size_t>(source)];
}

std::vector<int64_t> Dag::kthPath(int64_t source, int64_t target, const BigUint& k) const {
    checkEndpoint(source);
    checkEndpoint(target);
    std::vector<BigUint> count = countsTo(target);
    BigUint total = count[static_cast<size_t>(source)];
    if (k.isZero() || k > total) {
        throw DomainError("K_OUT_OF_RANGE",
                          "k=" + k.toString() + " out of range [1," + total.toString() + "]");
    }
    BigUint remaining = k;
    std::vector<int64_t> path;
    int64_t cur = source;
    while (true) {
        path.push_back(cur);
        if (cur == target) return path;
        for (int64_t v : adj_[static_cast<size_t>(cur)]) {
            const BigUint& c = count[static_cast<size_t>(v)];
            if (c.isZero()) continue;
            if (remaining > c) {
                remaining -= c;
            } else {
                cur = v;
                break;
            }
        }
    }
}

std::vector<std::vector<int64_t>> Dag::enumeratePaths(
    int64_t source, int64_t target, int64_t cap, bool& truncated) const {
    checkEndpoint(source);
    checkEndpoint(target);
    truncated = false;
    std::vector<std::vector<int64_t>> out;
    std::vector<int64_t> path{source};
    // Iterative DFS visiting successors in ascending id order, which yields
    // paths in lexicographic order.
    std::vector<size_t> nextIdx{0};
    while (!path.empty()) {
        int64_t u = path.back();
        if (u == target) {
            out.push_back(path);
            if (static_cast<int64_t>(out.size()) >= cap) {
                truncated = true;
                return out;
            }
            path.pop_back();
            nextIdx.pop_back();
            continue;
        }
        const auto& succ = adj_[static_cast<size_t>(u)];
        size_t& i = nextIdx.back();
        if (i < succ.size()) {
            path.push_back(succ[i++]);
            nextIdx.push_back(0);
        } else {
            path.pop_back();
            nextIdx.pop_back();
        }
    }
    return out;
}
