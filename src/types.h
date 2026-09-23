#pragma once
// Core data model for the frozen pgo-input/1.0 protocol.
#include <array>
#include <map>
#include <string>
#include <vector>

namespace pgo {

constexpr const char* kInputProtocol  = "pgo-input/1.0";
constexpr const char* kResultProtocol = "pgo-result/1.0";

// SE(2) pose: x, y, theta (radians; normalized to (-pi, pi] on load).
struct Pose {
  double x = 0.0;
  double y = 0.0;
  double theta = 0.0;
};

struct Node {
  std::string id;
  Pose init;
  bool fixed_hint = false;  // explicit "fixed": true in input
};

struct Edge {
  std::string id;
  std::string from;
  std::string to;
  Pose measurement;                                  // relative pose z_{from->to}
  std::array<double, 9> information{};               // row-major 3x3
  std::string loss_type = "default";                 // default|none|huber|cauchy
  double loss_param = 0.0;                           // 0 = use global setting
};

enum class AnchorMode {
  Single,        // graph must be one connected component; one anchor total
  PerComponent,  // one anchor per connected component
};

struct Options {
  AnchorMode anchor_mode = AnchorMode::Single;
  std::string global_loss_type = "huber";  // huber|cauchy|none
  double global_loss_param = 1.0;
  int max_iterations = 100;
  double function_tolerance = 1e-10;
  double gradient_tolerance = 1e-10;
  double parameter_tolerance = 1e-10;
  std::string linear_solver = "sparse_cholesky";  // sparse_cholesky|qr
  int num_threads = 1;
  bool save_initial = true;
  // Cancellation: abort the solve after this many milliseconds of wall clock
  // (0 disables). SIGTERM/SIGINT also request cancellation at any time.
  int cancel_after_ms = 0;
  int sleep_before_ms = 0;  // pause before building the problem (test hook)
};

struct Graph {
  int protocol_version_major = 1;
  std::string protocol_version = "1.0";
  std::string graph_version;  // required: freezes the input graph version
  std::string name;
  std::map<std::string, Node> nodes;
  std::vector<Edge> edges;
  Options options;
};

// Per-edge residual in local edge frame, raw and weighted norms, robust cost.
struct EdgeError {
  std::string edge_id;
  std::string from;
  std::string to;
  std::array<double, 3> raw_error{};       // [dx, dy, dtheta(already wrapped)]
  double raw_norm = 0.0;                   // ||e||_2
  double weighted_squared = 0.0;           // e^T Sigma^-1 e
  double robust_cost = 0.0;                // rho(e^T Sigma^-1 e)
};

struct ValidationIssue {
  std::string severity;  // error|warning
  std::string code;
  std::string message;
};

struct ValidationReport {
  bool ok = true;
  std::vector<ValidationIssue> issues;
  std::vector<std::vector<std::string>> components;  // connected components
};

}  // namespace pgo
