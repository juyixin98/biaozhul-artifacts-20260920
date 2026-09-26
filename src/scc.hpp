#ifndef SCC_SCC_HPP
#define SCC_SCC_HPP

#include <utility>
#include <vector>

#include "json.hpp"

namespace scc {

// Scale limits. Requests beyond these are rejected with an error.
inline constexpr int64_t kMaxVertices = 100000;
inline constexpr int64_t kMaxEdges = 2000000;
// The naive O(V^3/64) reachability reference is only allowed up to this size.
inline constexpr int64_t kReferenceMaxVertices = 3000;

struct Graph {
  int vertexCount = 0;
  // Adjacency lists: sorted ascending, duplicates removed (deterministic
  // traversal). Self-loops are preserved.
  std::vector<std::vector<int>> adj;
  int64_t rawEdgeCount = 0;  // edge count as given in the request (with dups)
};

struct ComponentResult {
  int id = 0;
  std::vector<int> vertices;     // sorted ascending
  std::vector<int> cycleWitness; // simple cycle v0..vk,v0; empty when none
};

struct SccResult {
  std::vector<ComponentResult> components;
  // Deduplicated cross-component edges (compU, compV), sorted, compU != compV.
  std::vector<std::pair<int, int>> condensationEdges;
  std::vector<int> vertexToComponent;
};

// Parses and validates a request JSON object into a Graph.
// Throws JsonError on any schema/range/limit violation.
Graph graphFromJson(const Json& req);

// Tarjan's SCC (iterative, no recursion depth limit). Deterministic:
// components are numbered 0..k-1 ordered by their smallest vertex id.
SccResult computeScc(const Graph& g);

// Naive reference: bitset transitive closure (reachability matrix), SCCs are
// mutual-reachability classes. Only for graphs up to kReferenceMaxVertices.
SccResult computeSccReference(const Graph& g);

// Serializes a result to the response JSON object.
Json resultToJson(const SccResult& r, const Graph& g, bool usedReference);

}  // namespace scc

#endif  // SCC_SCC_HPP
