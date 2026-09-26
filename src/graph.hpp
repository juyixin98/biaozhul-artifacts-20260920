// Directed multigraph with vertex ids fixed by input order [0, n).
#pragma once

#include <cstdint>
#include <string>
#include <vector>

struct Edge {
    int from;
    int to;
    std::int64_t multiplicity; // number of identical (from,to) edges
};

struct Graph {
    int n = 0;
    std::vector<std::string> labels;                    // n entries
    std::vector<std::vector<std::pair<int, std::int64_t>>> adj;  // adj[u]: (v, multiplicity)
    std::vector<std::vector<std::pair<int, std::int64_t>>> radj; // reverse graph
    std::vector<Edge> uniqueEdges;                      // unique directed edges, sorted
    std::int64_t totalRawEdges = 0;
};

// Size limits (see README, "规模限定" section).
namespace limits {
constexpr int MAX_N = 100000;
constexpr std::int64_t MAX_RAW_EDGES = 1000000;
constexpr int MAX_NAIVE_N = 64; // naive O(n^3) reachability reference cutoff
} // namespace limits

struct Component {
    int id;
    std::vector<int> vertices; // sorted ascending
    int representative;        // smallest vertex id in the component
};

struct DagEdge {
    int fromComponent;
    int toComponent;
    std::int64_t multiplicity; // total number of raw edges crossing this SCC pair
};

struct AnalysisResult {
    std::vector<Component> components; // ordered by component id
    std::vector<int> compOf;           // vertex -> component id
    std::vector<DagEdge> dagEdges;     // sorted by (fromComponent, toComponent)
    // Per-component cycle witness: empty for singleton acyclic components.
    // witness[i] is a list of vertex ids forming a closed walk v0..vk=v0,
    // encoded without repeating the closing vertex.
    std::vector<std::vector<int>> witnesses;
};
