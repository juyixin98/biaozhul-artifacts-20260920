// SE(2) pose graph optimization with Ceres.
#ifndef PGO_OPTIMIZER_HPP
#define PGO_OPTIMIZER_HPP

#include <array>
#include <string>
#include <vector>

#include "graph.hpp"

namespace pgo {

enum class RobustLossKind { None, Huber, Cauchy };

struct OptimizeOptions {
  int maxIterations = 100;
  RobustLossKind robustLoss = RobustLossKind::Huber;
  double robustLossScale = 1.0;
  bool useSparseLinearSolver = true;
  int numThreads = 4;
  bool fixedThreads = false;  // true for deterministic acceptance tests
  // Node ids pinned to their initial values. Empty => per-component anchors
  // are chosen automatically. All components must have at least one anchor.
  std::vector<std::string> fixedAnchors;
};

struct EdgeError {
  std::string edgeId;
  std::string from;
  std::string to;
  // Raw 3-vector residual (dx_err, dy_err, dtheta_err wrapped to [-pi,pi]).
  std::array<double, 3> residual{};
  // Weighted Mahalanobis norm squared: e^T I e (without robust reduction).
  double chi2 = 0.0;
  // Robust influence weight w with rho'(s) = 2 w: chi2_robust = w * chi2.
  double robustWeight = 1.0;
};

struct OptimizeResult {
  bool converged = false;
  bool cancelled = false;
  std::string termination;  // Ceres termination type, human-readable
  std::string message;
  int iterations = 0;

  double initialChi2 = 0.0;   // sum e^T I e, before optimization
  double finalChi2 = 0.0;     // sum e^T I e, after optimization
  double initialCost = 0.0;   // Ceres cost (0.5 * robust chi2) before
  double finalCost = 0.0;     // Ceres cost (0.5 * robust chi2) after
  double initialRms = 0.0;    // RMS of unweighted residual components, before
  double finalRms = 0.0;      // RMS of unweighted residual components, after

  std::vector<std::string> anchors;        // node ids actually fixed
  std::vector<int> anchorNodeIndices;
  std::vector<EdgeError> edgeErrorsBefore;
  std::vector<EdgeError> edgeErrorsAfter;
};

// Runs the optimization. poses [3*i + 0..2] are mutable node poses; on
// cancellation result.cancelled is set and poses are left unspecified
// (callers MUST NOT publish them).
OptimizeResult optimizePoseGraph(const Graph& graph,
                                 std::vector<double>& poses,
                                 const OptimizeOptions& options);

const char* robustLossKindName(RobustLossKind k);
RobustLossKind parseRobustLossKind(const std::string& s);

}  // namespace pgo

#endif
