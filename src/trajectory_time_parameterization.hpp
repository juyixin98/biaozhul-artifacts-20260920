// trajectory_time_parameterization.hpp
//
// Offline multi-joint path time parameterization.
//
// Input:
//   - discrete joint-space waypoints  q_0, q_1, ..., q_{n-1}  (n x dof)
//   - per-joint velocity limits      v_max (dof, strictly positive)
//   - per-joint acceleration limits  a_max (dof, strictly positive)
//
// Output:
//   - strictly increasing time stamps t_i
//   - position / velocity / acceleration state at every waypoint
//   - per-segment bottleneck information (which joint and which limit binds)
//   - dense within-segment samples used to numerically verify constraints
//
// Interpolation (stated explicitly, see README "Algorithm" section):
//   * Positions: piecewise LINEAR in a path parameter s, where s is the
//     Euclidean joint-space arc length measured from the path start:
//         q(s) = q_k + u_k * (s - s_k),  u_k = (q_{k+1}-q_k)/L_k,
//     for s in [s_k, s_{k+1}], L_k = ||q_{k+1}-q_k||_2.
//   * Time law: a time-optimal TRAPEZOIDAL (bang-coast-bang / triangular
//     when the segment is too short to reach the ceiling) speed profile
//     sd(s) = ds/dt per segment, with sd = 0 at every interior waypoint
//     (a corner where the path direction jumps), and optional requested
//     start/end speeds on the first/last segment.
//
//   Therefore q(t) is linear-per-segment, velocities are piecewise linear
//   (constant acceleration inside each phase), and all limits are satisfied
//   on the continuous trajectory, not only at the waypoints.  Dense samples
//   of the *continuous* law are emitted and checked.
//
// Zero-length segments (consecutive identical waypoints) are kept as dwell
// segments of configurable positive duration so that the time vector stays
// strictly increasing; they are also reported in diagnostics.
#pragma once

#include <Eigen/Dense>
#include <string>
#include <vector>

namespace jtp {

struct SegmentInfo {
  int index = 0;                 // segment index in the ORIGINAL input
  bool zero_length = false;      // duplicate point / zero-length dwell
  double length = 0.0;           // L_k = joint-space Euclidean length
  double duration = 0.0;         // assigned segment duration (dwell_time if zero length)
  double v_path_max = 0.0;       // ceiling  V_k = min_j v_max_j/|u_kj|
  double a_path_max = 0.0;       //         A_k = min_j a_max_j/|u_kj|
  // Bottleneck joint for the velocity ceiling (-1 if zero-length / no moving joint).
  int velocity_bottleneck_joint = -1;
  // Bottleneck joint for the acceleration ceiling (-1 if zero-length).
  int acceleration_bottleneck_joint = -1;
  // Actual limiting regime: "acceleration" (triangular, peak < ceiling),
  // "velocity" (trapezoid with cruise), or "dwell" (zero-length segment).
  std::string binding_regime;
  double peak_path_speed = 0.0;  // actual sd peak reached
  // Per-axis utilization at the binding instant (v_j / v_max_j etc.), informative.
  double bottleneck_velocity_ratio = 0.0;
  double bottleneck_accel_ratio = 0.0;
};

struct WaypointState {
  double time = 0.0;
  Eigen::VectorXd position;
  Eigen::VectorXd velocity;
  Eigen::VectorXd acceleration; // acceleration leaving the waypoint (segment k),
                                // i.e. left-hand derivative of the segment phase.
};

struct DenseSample {
  double time;
  Eigen::VectorXd position;
  Eigen::VectorXd velocity;
  Eigen::VectorXd acceleration;
  int segment;
};

struct VerificationReport {
  int samples_per_segment = 0;
  double velocity_tolerance = 0.0;
  double acceleration_tolerance = 0.0;
  bool passed = false;
  double max_velocity_violation = 0.0;  // max_j,s (|v|-v_max), <= tol if pass
  double max_acceleration_violation = 0.0;
  int worst_velocity_joint = -1;
  int worst_acceleration_joint = -1;
  double time_min_gap = 0.0;            // minimum consecutive time gap (>0)
  int total_samples = 0;
};

struct ParameterizeRequest {
  std::vector<Eigen::VectorXd> waypoints;   // n x dof
  Eigen::VectorXd velocity_limits;          // dof
  Eigen::VectorXd acceleration_limits;      // dof
  // Optional requested Cartesian joint velocities at path start / end.
  // Empty means zero. Must be parallel to the first/last segment direction.
  Eigen::VectorXd start_velocity;
  Eigen::VectorXd end_velocity;
  double dwell_time = 1e-3;                 // duration assigned to zero-length segments
  int samples_per_segment = 201;            // dense verification samples (>=2)
};

struct ParameterizeResult {
  int dof = 0;
  std::vector<WaypointState> states;        // n states, strictly increasing times
  std::vector<SegmentInfo> segments;        // n-1 segments
  VerificationReport verification;
  // Indices of consecutive duplicate points in the ORIGINAL input (i -> i+1).
  std::vector<int> zero_length_segments;
  std::string interpolation;                // human-readable method description
};

struct ApiError {
  int status = 400;          // HTTP status mirror
  std::string code;          // machine-readable, e.g. "INVALID_LIMITS"
  std::string message;       // human-readable
};

// Thrown as ApiError for malformed input / genuinely infeasible boundary data.
class ParameterizeException : public std::exception {
 public:
  explicit ParameterizeException(ApiError e) : error_(std::move(e)) {}
  const ApiError& error() const { return error_; }
  const char* what() const noexcept override { return error_.message.c_str(); }

 private:
  ApiError error_;
};

// Run the full time parameterization. Never returns partial results:
// throws ParameterizeException on invalid or infeasible input.
ParameterizeResult Parameterize(const ParameterizeRequest& req);

}  // namespace jtp
