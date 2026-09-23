// In-memory pose graph model + JSON loader with strict validation.
#ifndef PGO_GRAPH_HPP
#define PGO_GRAPH_HPP

#include <array>
#include <map>
#include <stdexcept>
#include <string>
#include <vector>

namespace pgo {

class JsonValue;

inline constexpr const char* kFormatVersion = "1.0";
inline constexpr int kInfoDim = 3;

struct Node {
  std::string id;
  double x = 0.0;
  double y = 0.0;
  double theta = 0.0;  // initial guess, radians, may be outside [-pi, pi]
};

struct Edge {
  std::string id;
  std::string from;
  std::string to;
  double dx = 0.0;
  double dy = 0.0;
  double dtheta = 0.0;
  // Row-major 3x3 information matrix (inverse covariance), must be SPD.
  std::array<double, 9> info{};
};

struct Graph {
  std::string formatVersion;
  std::string graphName;
  std::vector<Node> nodes;
  std::vector<Edge> edges;
  // Map ids -> node index, filled by loadGraph.
  std::map<std::string, int> indexById;
};

class ValidationError : public std::runtime_error {
 public:
  explicit ValidationError(const std::string& msg) : std::runtime_error(msg) {}
};

// Loads and validates a graph from a parsed JSON document.
// Throws ValidationError on any schema / value problem (including non-SPD
// information matrices), std::exception on JSON type problems.
Graph loadGraph(const JsonValue& root);

// Connected-component labeling on the undirected graph built from edges.
// Returns component id per node index, and the number of components.
struct Components {
  std::vector<int> componentOf;  // size = nodes.size()
  int count = 0;
  std::vector<std::vector<int>> members;  // members[component] -> node indices
};
Components labelComponents(const Graph& g);

}  // namespace pgo

#endif
