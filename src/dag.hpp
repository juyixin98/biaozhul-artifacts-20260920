// dag.hpp — DAG path counting, K-th lexicographic path, naive enumeration.
//
// Path order: paths from S to T are ordered lexicographically by their
// node-ID sequence (integer IDs compared numerically). Because the graph
// is acyclic, every S-T walk visits T exactly once (at its end), so no
// path is a proper prefix of another and the lexicographic order is total.
//
// Counts and the rank K are arbitrary-precision (BigUint): no overflow.
#pragma once

#include <cstdint>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

#include "bigint.hpp"

// Scale limits (documented in README; exceeded input is rejected, not
// silently truncated).
inline constexpr int64_t kMaxNodes = 100000;
inline constexpr int64_t kMaxEdges = 1000000;
inline constexpr int64_t kMaxEnumerateCap = 100000;  // hard ceiling for max_paths

// Domain error carrying a stable machine-readable code.
struct DomainError : std::runtime_error {
    std::string code;
    DomainError(std::string c, std::string msg)
        : std::runtime_error(msg), code(std::move(c)) {}
};

class Dag {
public:
    // Validates the graph and throws DomainError on:
    //   INVALID_GRAPH   — bad node count, node id out of range, self-loop
    //   LIMIT_EXCEEDED  — too many nodes or edges
    //   DUPLICATE_EDGE  — the same (u,v) edge listed twice
    //   CYCLE           — the graph is not acyclic
    Dag(int64_t numNodes, const std::vector<std::pair<int64_t, int64_t>>& edges);

    int64_t numNodes() const { return numNodes_; }

    // Number of distinct paths from source to target (0 when unreachable).
    // source == target yields exactly one path: the trivial path [source].
    BigUint countPaths(int64_t source, int64_t target) const;

    // The k-th path (1-based) in lexicographic node-ID order.
    // Throws DomainError(K_OUT_OF_RANGE) when k < 1 or k > count.
    std::vector<int64_t> kthPath(int64_t source, int64_t target, const BigUint& k) const;

    // Naive reference: DFS enumeration of all paths in lexicographic order,
    // stopping after `cap` paths. Sets `truncated` when more paths exist.
    // Intended for small graphs; the caller bounds `cap`.
    std::vector<std::vector<int64_t>> enumeratePaths(
        int64_t source, int64_t target, int64_t cap, bool& truncated) const;

private:
    int64_t numNodes_;
    std::vector<std::vector<int64_t>> adj_;  // sorted ascending by node id
    std::vector<int64_t> topo_;              // Kahn order, built in ctor

    // count[v] = number of paths from v to target; 0 when unreachable.
    std::vector<BigUint> countsTo(int64_t target) const;
    void checkEndpoint(int64_t node) const;
};
