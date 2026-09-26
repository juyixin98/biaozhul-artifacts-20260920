// Undirected graph with bitset adjacency, parsed from JSON requests.
#pragma once

#include <cstdint>
#include <stdexcept>
#include <string>
#include <vector>

#include "json.hpp"

namespace tdw {

inline constexpr int MAX_VERTICES = 64;

struct Graph {
    int n = 0;
    // adj_[v] bit u == 1  <=>  {u,v} is an edge (u < v and v < u both set).
    std::vector<uint64_t> adj;

    bool hasEdge(int u, int v) const {
        return (adj[u] >> v) & 1ULL;
    }
    int degree(int v, uint64_t alive) const {
        return __builtin_popcountll(adj[v] & alive);
    }
    int degree(int v) const {
        return __builtin_popcountll(adj[v]);
    }
};

struct InputError : std::runtime_error {
    using std::runtime_error::runtime_error;
};

// Parses:
//   {"num_vertices": n, "edges": [[u,v], ...]}
// or {"n": n, "edges": ...}. Duplicate edges are rejected. Self loops are
// rejected. Vertices outside [0,n) are rejected.
Graph parseGraph(const Json& req);

// Builds the JSON object describing the input graph (echoed in responses).
Json graphToJson(const Graph& g);

} // namespace tdw
