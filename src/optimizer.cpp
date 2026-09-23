#include "optimizer.h"

#include <ceres/ceres.h>

#include <Eigen/Cholesky>
#include <Eigen/Dense>

#include <chrono>
#include <cmath>
#include <map>

#include "util.h"

namespace pgo {

namespace {

// Residual: sqrt-information weighted SE(2) relative-pose error.
class SE2EdgeCost {
 public:
  SE2EdgeCost(const Pose& z, const Eigen::Matrix3d& sqrt_info)
      : z_(z), sqrt_info_(sqrt_info) {}

  template <typename T>
  bool operator()(const T* const p1, const T* const p2, T* residuals) const {
    const T dx = p2[0] - p1[0];
    const T dy = p2[1] - p1[1];
    const T cf = ceres::cos(p1[2]);
    const T sf = ceres::sin(p1[2]);
    // R(from)^T (t_to - t_from)
    const T px = cf * dx + sf * dy;
    const T py = -sf * dx + cf * dy;
    const T rx = px - T(z_.x);
    const T ry = py - T(z_.y);
    const T cz = T(std::cos(z_.theta));
    const T sz = T(std::sin(z_.theta));
    // R(z)^T
    const T ex = cz * rx + sz * ry;
    const T ey = -sz * rx + cz * ry;
    T eth = (p2[2] - p1[2]) - T(z_.theta);
    eth = ceres::atan2(ceres::sin(eth), ceres::cos(eth));  // wrap to (-pi,pi]

    const T e[3] = {ex, ey, eth};
    for (int i = 0; i < 3; ++i) {
      residuals[i] = T(0);
      for (int j = 0; j < 3; ++j)
        residuals[i] += T(sqrt_info_(i, j)) * e[j];
    }
    return true;
  }

  static ceres::CostFunction* Create(const Pose& z,
                                     const Eigen::Matrix3d& sqrt_info) {
    return new ceres::AutoDiffCostFunction<SE2EdgeCost, 3, 3, 3>(
        new SE2EdgeCost(z, sqrt_info));
  }

 private:
  Pose z_;
  Eigen::Matrix3d sqrt_info_;
};

class CancelCallback : public ceres::IterationCallback {
 public:
  CancelCallback(std::chrono::steady_clock::time_point start,
                 int cancel_after_ms)
      : start_(start), cancel_after_ms_(cancel_after_ms) {}

  ceres::CallbackReturnType operator()(const ceres::IterationSummary&) override {
    if (IsCancelRequested()) return ceres::SOLVER_ABORT;
    if (cancel_after_ms_ > 0) {
      long long el =
          std::chrono::duration_cast<std::chrono::milliseconds>(
              std::chrono::steady_clock::now() - start_)
              .count();
      if (el >= cancel_after_ms_) return ceres::SOLVER_ABORT;
    }
    return ceres::SOLVER_CONTINUE;
  }

 private:
  std::chrono::steady_clock::time_point start_;
  int cancel_after_ms_;
};

ceres::LossFunction* MakeLoss(const std::string& type, double param) {
  if (type == "none") return nullptr;
  if (type == "cauchy") return new ceres::CauchyLoss(param);
  return new ceres::HuberLoss(param);  // huber is the default
}

}  // namespace

double RobustRho(const std::string& type, double a, double s) {
  if (s <= 0.0) return 0.0;
  if (type == "none") return s;
  if (type == "cauchy") return a * a * std::log1p(s / (a * a));
  // huber
  if (s <= a * a) return s;
  return 2.0 * a * std::sqrt(s) - a * a;
}

CostSummary EvaluateEdges(const Graph& g,
                          const std::vector<std::string>& node_order,
                          const std::vector<Pose>& poses,
                          std::vector<EdgeError>* errors) {
  std::map<std::string, size_t> idx;
  for (size_t i = 0; i < node_order.size(); ++i) idx[node_order[i]] = i;

  CostSummary sum;
  if (errors) errors->clear();
  for (const Edge& e : g.edges) {
    const Pose& p1 = poses[idx[e.from]];
    const Pose& p2 = poses[idx[e.to]];
    std::array<double, 3> r = EdgeResidual(p1, p2, e.measurement);

    Eigen::Map<const Eigen::Matrix3d> M(e.information.data());
    Eigen::Vector3d ev(r.data());
    double ws = ev.transpose() * M * ev;

    std::string lt = e.loss_type;
    double lp = e.loss_param;
    if (lt == "default") lt = g.options.global_loss_type;
    if (lp <= 0.0) lp = g.options.global_loss_param;
    double rc = 0.5 * RobustRho(lt, lp, ws);

    EdgeError ee;
    ee.edge_id = e.id;
    ee.from = e.from;
    ee.to = e.to;
    ee.raw_error = r;
    ee.raw_norm = std::sqrt(r[0] * r[0] + r[1] * r[1] + r[2] * r[2]);
    ee.weighted_squared = ws;
    ee.robust_cost = rc;
    if (errors) errors->push_back(ee);
    sum.weighted_cost += 0.5 * ws;
    sum.raw_squared += r[0] * r[0] + r[1] * r[1] + r[2] * r[2];
    sum.robust_cost += rc;
  }
  return sum;
}

OptimizeOutcome Optimize(const Graph& g,
                         const std::vector<std::string>& anchors,
                         const Options& opts) {
  OptimizeOutcome out;
  out.max_iterations = opts.max_iterations;

  std::vector<std::string> order;
  order.reserve(g.nodes.size());
  for (const auto& kv : g.nodes) order.push_back(kv.first);

  std::vector<Pose> poses(order.size());
  for (size_t i = 0; i < order.size(); ++i)
    poses[i] = g.nodes.at(order[i]).init;

  out.initial = EvaluateEdges(g, order, poses, &out.initial_errors);

  ceres::Problem problem;
  std::vector<double*> blocks(order.size(), nullptr);
  for (size_t i = 0; i < order.size(); ++i)
    blocks[i] = &poses[i].x;  // x, y, theta are contiguous in Pose

  std::map<std::string, size_t> pos;
  for (size_t i = 0; i < order.size(); ++i) pos[order[i]] = i;

  for (const Edge& e : g.edges) {
    Eigen::Map<const Eigen::Matrix3d> M(e.information.data());
    Eigen::Matrix3d sym = 0.5 * (M + M.transpose());
    Eigen::LLT<Eigen::Matrix3d> llt(sym);
    Eigen::Matrix3d sqrt_info = llt.matrixL().transpose();  // L^T L = M

    std::string lt = e.loss_type;
    double lp = e.loss_param;
    if (lt == "default") lt = opts.global_loss_type;
    if (lp <= 0.0) lp = opts.global_loss_param;

    problem.AddResidualBlock(
        SE2EdgeCost::Create(e.measurement, sqrt_info), MakeLoss(lt, lp),
        blocks[pos[e.from]], blocks[pos[e.to]]);
  }

  // Anchors fix the gauge (must be set after blocks are added by residuals).
  for (const std::string& a : anchors) {
    double* blk = blocks[pos[a]];
    if (!problem.HasParameterBlock(blk))
      problem.AddParameterBlock(blk, 3);
    problem.SetParameterBlockConstant(blk);
  }

  ceres::Solver::Options so;
  so.max_num_iterations = opts.max_iterations;
  so.function_tolerance = opts.function_tolerance;
  so.gradient_tolerance = opts.gradient_tolerance;
  so.parameter_tolerance = opts.parameter_tolerance;
  so.num_threads = std::max(1, opts.num_threads);
  so.minimizer_progress_to_stdout = false;
  so.linear_solver_type = opts.linear_solver == "qr"
                              ? ceres::DENSE_QR
                              : ceres::SPARSE_NORMAL_CHOLESKY;

  SleepMs(opts.sleep_before_ms);
  if (IsCancelRequested()) {
    out.cancelled = true;
    out.message = "cancelled before solve";
    return out;
  }

  auto start = std::chrono::steady_clock::now();
  CancelCallback cb(start, opts.cancel_after_ms);
  so.callbacks.push_back(&cb);
  so.update_state_every_iteration = true;

  ceres::Solver::Summary summary;
  ceres::Solve(so, &problem, &summary);
  out.elapsed_ms =
      std::chrono::duration<double, std::milli>(
          std::chrono::steady_clock::now() - start)
          .count();
  out.iterations = static_cast<int>(summary.iterations.size());
  out.termination = ceres::TerminationTypeToString(summary.termination_type);
  out.message = summary.message;

  if (summary.termination_type == ceres::USER_FAILURE ||
      IsCancelRequested()) {
    // Never publish partial state: optimized/final stay empty.
    out.cancelled = true;
    out.message = "solve aborted by cancellation request";
    return out;
  }

  out.converged = summary.termination_type == ceres::CONVERGENCE;
  out.optimized = poses;
  out.node_order = order;
  out.final = EvaluateEdges(g, order, poses, &out.final_errors);
  return out;
}

}  // namespace pgo
