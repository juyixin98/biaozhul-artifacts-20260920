// SPDX-License-Identifier: MIT
#include "graph.h"

#include <algorithm>
#include <functional>
#include <numeric>
#include <queue>
#include <unordered_set>

namespace dagpaths {

Graph Graph::build(int nodeCount, std::vector<Edge> edges) {
    if (nodeCount < 1)
        throw std::invalid_argument("node count must be >= 1");
    if (nodeCount > MAX_NODES)
        throw std::invalid_argument("node count exceeds limit of "
                                    + std::to_string(MAX_NODES));
    if (static_cast<int>(edges.size()) > MAX_EDGES)
        throw std::invalid_argument("edge count exceeds limit of "
                                    + std::to_string(MAX_EDGES));

    for (const Edge& e : edges) {
        if (e.first < 0 || e.first >= nodeCount ||
            e.second < 0 || e.second >= nodeCount) {
            throw std::invalid_argument(
                "edge endpoint out of range: ["
                + std::to_string(e.first) + ", "
                + std::to_string(e.second) + "]");
        }
        if (e.first == e.second)
            throw std::invalid_argument(
                "self-loop is not allowed in a DAG: "
                + std::to_string(e.first));
    }

    std::sort(edges.begin(), edges.end());
    const auto duplicate = std::adjacent_find(edges.begin(), edges.end());
    if (duplicate != edges.end())
        throw std::invalid_argument(
            "duplicate edge: [" + std::to_string(duplicate->first) + ", "
            + std::to_string(duplicate->second) + "]");

    std::vector<std::vector<int>> outAdj(nodeCount);
    std::vector<std::vector<int>> inAdj(nodeCount);
    std::vector<int> indegree(nodeCount, 0);
    for (const Edge& e : edges) {
        outAdj[e.first].push_back(e.second);
        inAdj[e.second].push_back(e.first);
        ++indegree[e.second];
    }
    // Edges are globally sorted, so each adjacency list is already in
    // ascending node-ID order; lexicographic path order relies on this.

    // Kahn's algorithm; topological order doubles as the cycle detector.
    std::queue<int> ready;
    for (int v = 0; v < nodeCount; ++v)
        if (indegree[v] == 0) ready.push(v);

    std::vector<int> topo;
    topo.reserve(nodeCount);
    while (!ready.empty()) {
        const int u = ready.front();
        ready.pop();
        topo.push_back(u);
        for (int v : outAdj[u])
            if (--indegree[v] == 0) ready.push(v);
    }
    if (static_cast<int>(topo.size()) != nodeCount) {
        // Identify one node on a cycle for an actionable error message.
        const auto it = std::find_if(indegree.begin(), indegree.end(),
                                     [](int d) { return d > 0; });
        throw std::invalid_argument(
            "graph contains a directed cycle; first detected node: "
            + std::to_string(static_cast<int>(it - indegree.begin())));
    }

    return Graph(nodeCount, std::move(outAdj), std::move(inAdj),
                 std::move(topo));
}

std::vector<BigInt> Graph::waysTo(int t) const {
    // Reverse topological sweep: ways[u] = sum of ways[v] over u -> v.
    // ways[t] = 1 represents the empty suffix path (single-node path t).
    std::vector<BigInt> ways(nodeCount_, BigInt::zero());
    ways[t] = BigInt::one();
    for (auto it = topo_.rbegin(); it != topo_.rend(); ++it) {
        const int u = *it;
        if (u == t) continue;
        BigInt sum = BigInt::zero();
        for (int v : outAdj_[u]) sum += ways[v];
        ways[u] = std::move(sum);
    }
    return ways;
}

BigInt Graph::countPaths(int s, int t) const {
    return waysTo(t)[s];
}

std::vector<int> Graph::kthPath(int s, int t, const BigInt& k) const {
    if (k < BigInt::one())
        throw std::out_of_range("k must be >= 1");
    const std::vector<BigInt> ways = waysTo(t);
    if (k > ways[s]) {
        throw std::out_of_range(
            "k out of range: k=" + k.str() + ", total paths="
            + ways[s].str());
    }
    std::vector<int> path;
    path.push_back(s);
    int cur = s;
    BigInt remaining = k;
    while (cur != t) {
        bool moved = false;
        for (int v : outAdj_[cur]) { // ascending node ID
            const BigInt subtree = ways[v];
            if (remaining <= subtree) {
                path.push_back(v);
                cur = v;
                moved = true;
                break;
            }
            remaining -= subtree; // skip v's whole lexicographic block
        }
        if (!moved)
            throw std::logic_error("kthPath walk stalled (internal error)");
    }
    return path;
}

BigInt Graph::rankOfPath(const std::vector<int>& path) const {
    if (path.empty())
        throw std::invalid_argument("path must contain at least one node");
    for (int node : path) {
        if (node < 0 || node >= nodeCount_)
            throw std::invalid_argument(
                "path references node outside graph: "
                + std::to_string(node));
    }
    const int t = path.back();
    const std::vector<BigInt> ways = waysTo(t);

    BigInt rank = BigInt::one();
    for (size_t i = 0; i + 1 < path.size(); ++i) {
        const int u = path[i];
        const int next = path[i + 1];
        // Every path branching to a smaller-ID successor precedes ours.
        bool edgeExists = false;
        for (int v : outAdj_[u]) {
            if (v == next) { edgeExists = true; break; }
            rank += ways[v]; // v < next because lists are ascending
        }
        if (!edgeExists)
            throw std::invalid_argument(
                "path is not valid: no edge " + std::to_string(u) + " -> "
                + std::to_string(next));
    }
    // In a DAG, consecutive valid edges already prove a valid s -> t path.
    return rank;
}

std::vector<std::vector<int>> Graph::enumeratePaths(int s, int t) const {
    std::vector<std::vector<int>> results;
    std::vector<int> prefix;
    prefix.push_back(s);

    std::function<void(int)> dfs = [&](int u) {
        if (u == t) {
            results.push_back(prefix);
            if (results.size() > ENUMERATION_CAP)
                throw std::length_error(
                    "enumeration exceeded cap of "
                    + std::to_string(ENUMERATION_CAP) + " paths");
            return;
        }
        for (int v : outAdj_[u]) { // ascending: results are lexicographic
            prefix.push_back(v);
            dfs(v);
            prefix.pop_back();
        }
    };
    dfs(s);
    return results;
}

} // namespace dagpaths
