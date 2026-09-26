#pragma once

#include <string>
#include <utility>
#include <vector>

#include "json.hpp"

// Undirected simple graph (no self-loops, parallel edges deduplicated).
// Vertices are 0..n-1 internally; labels preserve user-provided names.
struct Graph {
    std::vector<std::string> labels;
    std::vector<std::vector<char>> adj;

    int n() const { return static_cast<int>(labels.size()); }
    bool hasEdge(int u, int v) const { return adj[u][v] != 0; }

    long edgeCount() const {
        long count = 0;
        for (int u = 0; u < n(); ++u)
            for (int v = u + 1; v < n(); ++v)
                if (adj[u][v]) ++count;
        return count;
    }

    std::vector<std::pair<int, int>> edgeList() const {
        std::vector<std::pair<int, int>> out;
        for (int u = 0; u < n(); ++u)
            for (int v = u + 1; v < n(); ++v)
                if (adj[u][v]) out.emplace_back(u, v);
        return out;
    }
};

// Accepted graph specs (all describe the same undirected graph):
//   {"vertices": ["A", "B"], "edges": [["A", "B"]]}
//   {"n": 4, "edges": [[0, 1], [1, 2]]}
//   {"edges": [["A", "B"]]}                         (vertices inferred)
//   {"adjacency": {"A": ["B", "C"], "B": ["A"]}}
// Integer and string endpoints are both accepted; integer k is the label
// std::to_string(k). Throws std::runtime_error with a user-readable message.
Graph graphFromJson(const json::Value& spec);

// Serialize back to the canonical {"vertices": ..., "edges": ...} form.
json::Value graphToJson(const Graph& g);
