#include "optimizer.hpp"

#include <Eigen/Cholesky>
#include <Eigen/Core>
#include <ceres/ceres.h>
#include <ceres/loss_function.h>

#include <algorithm>
#include <cmath>
#include <set>
#include <stdexcept>
#include <string>

#include "se2.hpp"
#include "signal.hpp"

namespace pgo {

namespace {

// Residual: r = L * ( e_ij^{-1} ( x_i^{-1} x_j ) ) with L the Cholesky
// factor of the information matrix (I = L L^T). Theta component is wrapped
// across the +/-pi seam by atan2(sin, cos).
struct EdgeCost {
  EdgeCost(double dx, double dy, double dtheta, const Eigen::Matrix3d& L)
      : mx_(dx), my_(dy), mt_(dtheta), l00_(L(0, 0)),
        l10_(L(1, 0)), l11_(L(1, 1)),
        l20_(L(2, 0)), l21_(L(2, 1)), l22_(L(2, 2)) {}

  template <typename T>
  bool operator()(const T* const xi, const T* const xj, T* residuals) const {
    T inv[3];
    se2Inverse(xi, inv);
    T rel[3];
    se2Compose(inv, xj, rel);

    T ex = rel[0] - T(mx_);
    T ey = rel[1] - T(my_);
    T et = normalizeAngle(rel[2] - T(mt_));

    residuals[0] = T(l00_) * ex;
    residuals[1] = T(l10_) * ex + T(l11_) * ey;
    residuals[2] = T(l20_) * ex + T(l21_) * ey + T(l22_) * et;
    return true;
  }

  static ceres::CostFunction* create(double dx, double dy, double dtheta,
                                     const Eigen::Matrix3d& L) {
    return new ceres::AutoDiffCostFunction<EdgeCost, 3, 3, 3>(
        new EdgeCost(dx, dy, dtheta, L));
  }

 private:
  const double mx_, my_, mt_;
  const double l00_, l10_, l11_, l20_, l21_, l22_;
};

// Raw (unweighted) residual of one edge at the given poses, in double.
void rawResidual(const Graph& g, const Edge& e, const std::vector<double>& poses,
                 double raw[3]) {
  int i = g.indexById.at(e.from);
  int j = g.indexById.at(e.to);
  const double* xi = &poses[3 * i];
  const double* xj = &poses[3 * j];

  double inv[3], rel[3];
  se2Inverse(xi, inv);
  se2Compose(inv, xj, rel);
  raw[0] = rel[0] - e.dx;
  raw[1] = rel[1] - e.dy;
  raw[2] = normalizeAngle(rel[2] - e.dtheta);
}

// chi2 = raw^T info raw, robust weight for the configured loss, and RMS over
// all raw residual components (summed across edges).
struct ResidualStats {
  double chi2 = 0.0;
  double robustChi2 = 0.0;
  double rawSqSum = 0.0;
  int rawCount = 0;
};

double robustWeightFor(RobustLossKind kind, double scale, double chi2) {
  // Ceres applies cost = 0.5 * rho(r^T r). With its factor-of-two convention
  // rho'(s) = 2 w, so w = 0.5 * rho'(chi2).
  switch (kind) {
    case RobustLossKind::None:
      return 1.0;
    case RobustLossKind::Huber: {
      const double a = std::abs(scale);
      const double t = std::sqrt(std::max(0.0, chi2));
      return t <= a ? 1.0 : a / t;
    }
    case RobustLossKind::Cauchy:
      return 1.0 / (1.0 + chi2 / (scale * scale));
  }
  return 1.0;
}

ResidualStats computeStats(const Graph& g, const std::vector<double>& poses,
                           RobustLossKind kind, double scale,
                           std::vector<EdgeError>* perEdge) {
  ResidualStats st;
  if (perEdge) perEdge->clear();
  for (const Edge& e : g.edges) {
    double raw[3];
    rawResidual(g, e, poses, raw);
    Eigen::Map<const Eigen::Matrix<double, 3, 1>> r(raw);
    Eigen::Matrix3d info = Eigen::Matrix3d(e.info.data());
    double chi2 = r.transpose() * info * r;
    double w = robustWeightFor(kind, scale, chi2);
    st.chi2 += chi2;
    st.robustChi2 += w * chi2;
    st.rawSqSum += raw[0] * raw[0] + raw[1] * raw[1] + raw[2] * raw[2];
    st.rawCount += 3;
    if (perEdge) {
      EdgeError ee;
      ee.edgeId = e.id;
      ee.from = e.from;
      ee.to = e.to;
      ee.residual = {raw[0], raw[1], raw[2]};
      ee.chi2 = chi2;
      ee.robustWeight = w;
      perEdge->push_back(std::move(ee));
    }
  }
  return st;
}

class CancelCallback : public ceres::IterationCallback {
 public:
  explicit CancelCallback(Canceller* c) : canceller_(c) {}
  ceres::CallbackReturnType operator()(const ceres::IterationSummary&) override {
    return canceller_->cancelled() ? ceres::SOLVER_ABORT : ceres::SOLVER_CONTINUE;
  }

 private:
  Canceller* canceller_;
};

ceres::LossFunction* makeLoss(RobustLossKind kind, double scale) {
  switch (kind) {
    case RobustLossKind::None: return nullptr;
    case RobustLossKind::Huber: return new ceres::HuberLoss(scale);
    case RobustLossKind::Cauchy: return new ceres::CauchyLoss(scale);
  }
  return nullptr;
}

const char* terminationName(ceres::TerminationType t) {
  switch (t) {
    case ceres::CONVERGENCE: return "CONVERGENCE";
    case ceres::NO_CONVERGENCE: return "NO_CONVERGENCE";
    case ceres::FAILURE: return "FAILURE";
    case ceres::USER_SUCCESS: return "USER_SUCCESS";
    case ceres::USER_FAILURE: return "USER_FAILURE";
    default: return "UNKNOWN";
  }
}

}  // namespace

const char* robustLossKindName(RobustLossKind k) {
  switch (k) {
    case RobustLossKind::None: return "none";
    case RobustLossKind::Huber: return "huber";
    case RobustLossKind::Cauchy: return "cauchy";
  }
  return "huber";
}

RobustLossKind parseRobustLossKind(const std::string& s) {
  if (s == "none") return RobustLossKind::None;
  if (s == "huber") return RobustLossKind::Huber;
  if (s == "cauchy") return RobustLossKind::Cauchy;
  throw std::invalid_argument("unknown robust loss: " + s + " (expected none|huber|cauchy)");
}

OptimizeResult optimizePoseGraph(const Graph& graph,
                                 std::vector<double>& poses,
                                 const OptimizeOptions& options) {
  OptimizeResult result;
  const int n = static_cast<int>(graph.nodes.size());
  poses.assign(3 * n, 0.0);
  for (int i = 0; i < n; ++i) {
    poses[3 * i] = graph.nodes[i].x;
    poses[3 * i + 1] = graph.nodes[i].y;
    poses[3 * i + 2] = graph.nodes[i].theta;
  }

  // --- Anchor selection: every connected component must be anchored ---
  Components comps = labelComponents(graph);
  std::set<int> anchoredComponents;
  std::set<int> anchorIndices;

  for (const std::string& id : options.fixedAnchors) {
    auto it = graph.indexById.find(id);
    if (it == graph.indexById.end()) {
      throw std::invalid_argument("anchor node not found: " + id);
    }
    int idx = it->second;
    anchorIndices.insert(idx);
    anchoredComponents.insert(comps.componentOf[idx]);
  }

  if (options.fixedAnchors.empty()) {
    // Auto mode: smallest node index in each component => deterministic.
    for (int c = 0; c < comps.count; ++c) {
      int idx = *std::min_element(comps.members[c].begin(), comps.members[c].end());
      anchorIndices.insert(idx);
    }
  } else {
    for (int c = 0; c < comps.count; ++c) {
      if (!anchoredComponents.count(c)) {
        throw std::invalid_argument(
            "component " + std::to_string(c) + " (" +
            std::to_string(comps.members[c].size()) +
            " nodes) has no anchor; every connected component must be anchored");
      }
    }
  }
  for (int idx : anchorIndices) result.anchorNodeIndices.push_back(idx);
  for (int idx : result.anchorNodeIndices) result.anchors.push_back(graph.nodes[idx].id);
  std::sort(result.anchors.begin(), result.anchors.end());

  // Pre-solve stats (and per-edge errors).
  ResidualStats before = computeStats(graph, poses, options.robustLoss,
                                      options.robustLossScale,
                                      &result.edgeErrorsBefore);
  result.initialChi2 = before.chi2;
  result.initialCost = 0.5 * before.robustChi2;
  result.initialRms = std::sqrt(before.rawSqSum / before.rawCount);

  // --- Build Ceres problem ---
  ceres::Problem problem;
  // Register every parameter block explicitly so isolated nodes (components
  // with no edges at all) exist in the problem before they are anchored.
  for (int i = 0; i < n; ++i) {
    problem.AddParameterBlock(&poses[3 * i], 3);
  }
  ceres::LossFunction* loss = makeLoss(options.robustLoss, options.robustLossScale);
  for (const Edge& e : graph.edges) {
    Eigen::Matrix3d info = Eigen::Matrix3d(e.info.data());
    Eigen::LLT<Eigen::Matrix3d> llt(info);
    Eigen::Matrix3d L = llt.matrixL();  // re-validated here; loader already checked SPD
    int i = graph.indexById.at(e.from);
    int j = graph.indexById.at(e.to);
    problem.AddResidualBlock(EdgeCost::create(e.dx, e.dy, e.dtheta, L), loss,
                             &poses[3 * i], &poses[3 * j]);
  }
  for (int idx : result.anchorNodeIndices) {
    problem.SetParameterBlockConstant(&poses[3 * idx]);
  }

  ceres::Solver::Options solverOptions;
  solverOptions.max_num_iterations = options.maxIterations;
  solverOptions.linear_solver_type = options.useSparseLinearSolver
                                         ? ceres::SPARSE_NORMAL_CHOLESKY
                                         : ceres::DENSE_QR;
  solverOptions.num_threads = options.fixedThreads ? 1 : options.numThreads;
  solverOptions.minimizer_progress_to_stdout = false;
  solverOptions.function_tolerance = 1e-10;
  solverOptions.gradient_tolerance = 1e-12;

  Canceller* canceller = &Canceller::instance();
  CancelCallback callback(canceller);
  solverOptions.update_state_every_iteration = true;
  solverOptions.callbacks.push_back(&callback);

  // Cancellation requested before the solver ran (e.g. during JSON parse).
  // Initial stats above are still valid and get reported.
  if (canceller->cancelled()) {
    result.cancelled = true;
    result.termination = "CANCELLED_BEFORE_SOLVE";
    return result;
  }

  ceres::Solver::Summary summary;
  ceres::Solve(solverOptions, &problem, &summary);

  result.iterations = static_cast<int>(summary.iterations.size());
  result.termination = terminationName(summary.termination_type);
  result.message = summary.message;

  // The iteration callback returns SOLVER_ABORT -> USER_FAILURE. Also honor a
  // flag set between the last callback and now.
  if (summary.termination_type == ceres::USER_FAILURE || canceller->cancelled()) {
    result.cancelled = true;
    result.termination = result.iterations > 0 ? "CANCELLED_DURING_SOLVE"
                                               : "CANCELLED_BEFORE_SOLVE";
    return result;
  }

  result.converged = (summary.termination_type == ceres::CONVERGENCE);
  // NO_CONVERGENCE at max iterations is still a complete, usable result.

  ResidualStats after = computeStats(graph, poses, options.robustLoss,
                                     options.robustLossScale,
                                     &result.edgeErrorsAfter);
  result.finalChi2 = after.chi2;
  result.finalCost = 0.5 * after.robustChi2;
  result.finalRms = std::sqrt(after.rawSqSum / after.rawCount);
  return result;
}

}  // namespace pgo
