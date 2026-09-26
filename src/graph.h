// SPDX-License-Identifier: MIT
// DAG model and core algorithms: validation, topological ordering,
// path counting, lexicographic k-th path lookup and inverse ranking.
#ifndef DAGPATHS_GRAPH_H
#define DAGPATHS_GRAPH_H

#include <cstddef>
#include <string>
#include <utility>
#include <vector>

#include "bigint.h"

namespace dagpaths {

// Hard safety limits (documented in README). The algorithms themselves
// are exact; the caps only bound memory / request fan-out.
constexpr int MAX_NODES = 10000;
constexpr int MAX_EDGES = 50000;
constexpr std::size_t MAX_DIGITS_K = 10000; // k is a decimal string up to this many digits
constexpr std::size_t MAX_PAIRS = 100000;  // max source*sink pairs per request
constexpr std::size_t ENUMERATION_CAP = 100000; // naive enumerator safety cap

using Edge = std::pair<int, int>;

class Graph {
public:
    // Builds an immutable graph. Throws std::invalid_argument on any
    // structural problem: bad node count, out-of-range endpoint,
    // duplicate edge, or a directed cycle (self-loops included).
    static Graph build(int nodeCount, std::vector<Edge> edges);

    int nodeCount() const { return nodeCount_; }
    const std::vector<int>& topoOrder() const { return topo_; }
    const std::vector<std::vector<int>>& outAdj() const { return outAdj_; }
    const std::vector<std::vector<int>>& inAdj() const { return inAdj_; }

    // Number of directed paths s -> t. s == t counts the empty path once.
    // Exact arbitrary-precision result.
    BigInt countPaths(int s, int t) const;

    // Returns the k-th (1-based) path s -> t in lexicographic order of
    // node-ID sequences. Throws std::out_of_range if k < 1 or
    // k > countPaths(s, t).
    std::vector<int> kthPath(int s, int t, const BigInt& k) const;

    // 1-based lexicographic rank of an existing s -> t path.
    // Throws std::invalid_argument if the sequence is not a valid path.
    BigInt rankOfPath(const std::vector<int>& path) const;

    // Naive reference: enumerate every s -> t path in lexicographic order.
    // Throws std::length_error if more than ENUMERATION_CAP paths exist.
    std::vector<std::vector<int>> enumeratePaths(int s, int t) const;

private:
    Graph(int nodeCount,
          std::vector<std::vector<int>> outAdj,
          std::vector<std::vector<int>> inAdj,
          std::vector<int> topo)
        : nodeCount_(nodeCount),
          outAdj_(std::move(outAdj)),
          inAdj_(std::move(inAdj)),
          topo_(std::move(topo)) {}

    // ways[v] = number of paths from v to t, computed over a reverse
    // topological sweep. Returns the vector indexed by node id.
    std::vector<BigInt> waysTo(int t) const;

    int nodeCount_;
    std::vector<std::vector<int>> outAdj_;
    std::vector<std::vector<int>> inAdj_;
    std::vector<int> topo_;
};

} // namespace dagpaths

#endif
