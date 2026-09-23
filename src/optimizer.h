#pragma once
// Ceres-based 2D SE(2) pose graph optimizer with robust losses and
// interruptible solving.
#include <string>
#include <vector>

#include "types.h"

namespace pgo {

struct CostSummary {
  double weighted_cost = 0.0;  // sum of 0.5 * e^T Sigma^-1 e
  double raw_squared = 0.0;    // sum of ||e||^2
  double robust_cost = 0.0;    // sum of 0.5 * rho(e^T Sigma^-1 e)
};

struct OptimizeOutcome {
  bool converged = false;
  bool cancelled = false;
  std::string termination;   // Ceres termination type
  std::string message;
  int iterations = 0;
  int max_iterations = 0;
  double elapsed_ms = 0.0;
  std::vector<Pose> optimized;          // indexed by node order
  std::vector<std::string> node_order;  // node ids aligned with `optimized`
  std::vector<EdgeError> initial_errors;
  std::vector<EdgeError> final_errors;
  CostSummary initial;
  CostSummary final;
};

// Evaluate every edge at the given poses (pose index = position in node_order).
CostSummary EvaluateEdges(const Graph& g,
                          const std::vector<std::string>& node_order,
                          const std::vector<Pose>& poses,
                          std::vector<EdgeError>* errors);

// Robust rho(s) used by the backend, exposed for testing/reporting.
// huber: s<=a^2 -> s ; else 2 a sqrt(s) - a^2
// cauchy: a^2 log(1 + s/a^2)
// none: s
double RobustRho(const std::string& type, double param, double s);

// Run the optimization. `anchors` are fixed completely (gauge freedom).
// opts.cancel_after_ms and SIGTERM/SIGINT make the solve abort with
// cancelled=true; in that case `optimized` and `final_errors` are NOT
// populated and the caller must not publish them.
OptimizeOutcome Optimize(const Graph& g,
                         const std::vector<std::string>& anchors,
                         const Options& opts);

}  // namespace pgo
