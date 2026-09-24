#include "trajectory_time_parameterization.hpp"

#include <algorithm>
#include <cmath>
#include <limits>

namespace jtp {
namespace {

constexpr double kEps = 1e-12;

void Throw(int status, std::string code, std::string message) {
  throw ParameterizeException(ApiError{status, std::move(code), std::move(message)});
}

// Continuous trapezoidal speed-law state on one moving segment.
//
// Arc-length coordinate s in [0, L], path speed sd = ds/dt, path
// acceleration sdd = d2s/dt2.  Phase 1 accelerates at +A from s0 to the
// peak w; optional cruise at w; phase 3 decelerates at -A to s1.
struct SegmentLaw {
  double L = 0, A = 0, V = 0, w = 0, s0 = 0, s1 = 0;
  double t_acc = 0, t_cruise = 0, t_dec = 0;
  double d_acc = 0, d_cruise = 0;
  bool velocity_bound = false;

  double duration() const { return t_acc + t_cruise + t_dec; }

  // State (s, sd, sdd) at local segment time tau in [0, duration()].
  void eval(double tau, double& s, double& sd, double& sdd) const {
    if (tau < t_acc) {
      sdd = A;
      sd = s0 + A * tau;
      s = s0 * tau + 0.5 * A * tau * tau;
    } else if (tau < t_acc + t_cruise) {
      sdd = 0.0;
      sd = w;
      s = d_acc + w * (tau - t_acc);
    } else {
      sdd = -A;
      const double td = std::min(tau - t_acc - t_cruise, t_dec);
      sd = w - A * td;
      s = d_acc + d_cruise + w * td - 0.5 * A * td * td;
    }
  }
};

}  // namespace

ParameterizeResult Parameterize(const ParameterizeRequest& req) {
  // ---------------------------------------------------------------- validation
  const auto& wp = req.waypoints;
  if (wp.size() < 2) {
    Throw(422, "INVALID_WAYPOINTS",
          "at least 2 waypoints are required (got " + std::to_string(wp.size()) + ")");
  }
  const int dof = static_cast<int>(wp[0].size());
  if (dof < 1) {
    Throw(422, "INVALID_WAYPOINTS", "each waypoint must have at least 1 joint");
  }
  for (size_t i = 0; i < wp.size(); ++i) {
    if (wp[i].size() != dof) {
      Throw(422, "INVALID_WAYPOINTS",
            "waypoint " + std::to_string(i) + " has " + std::to_string(wp[i].size()) +
                " joints, expected " + std::to_string(dof));
    }
    if (!wp[i].allFinite()) {
      Throw(422, "INVALID_WAYPOINTS",
            "waypoint " + std::to_string(i) + " contains NaN or Inf");
    }
  }
  if (req.velocity_limits.size() != dof || req.acceleration_limits.size() != dof) {
    Throw(422, "INVALID_LIMITS",
          "velocity_limits and acceleration_limits must have length dof=" +
              std::to_string(dof));
  }
  for (int j = 0; j < dof; ++j) {
    if (!(std::isfinite(req.velocity_limits[j]) &&
          std::isfinite(req.acceleration_limits[j])) ||
        req.velocity_limits[j] <= 0.0 || req.acceleration_limits[j] <= 0.0) {
      Throw(422, "INVALID_LIMITS",
            "joint " + std::to_string(j) +
                " limits must be finite and strictly positive");
    }
  }
  if (!(std::isfinite(req.dwell_time) && req.dwell_time > 0.0)) {
    Throw(422, "INVALID_PARAMETERS", "dwell_time must be finite and strictly positive");
  }
  if (req.samples_per_segment < 2) {
    Throw(422, "INVALID_PARAMETERS", "samples_per_segment must be >= 2");
  }

  const bool has_v0 = req.start_velocity.size() > 0;
  const bool has_vn = req.end_velocity.size() > 0;
  if (has_v0 && req.start_velocity.size() != dof) {
    Throw(422, "INVALID_BOUNDARY_VELOCITY",
          "start_velocity must have length dof=" + std::to_string(dof));
  }
  if (has_vn && req.end_velocity.size() != dof) {
    Throw(422, "INVALID_BOUNDARY_VELOCITY",
          "end_velocity must have length dof=" + std::to_string(dof));
  }
  if (has_v0 && !req.start_velocity.allFinite()) {
    Throw(422, "INVALID_BOUNDARY_VELOCITY", "start_velocity contains NaN or Inf");
  }
  if (has_vn && !req.end_velocity.allFinite()) {
    Throw(422, "INVALID_BOUNDARY_VELOCITY", "end_velocity contains NaN or Inf");
  }

  const int n = static_cast<int>(wp.size());

  // -------------------------------------------- segment geometry and ceilings
  std::vector<Eigen::VectorXd> dir(n - 1);
  std::vector<SegmentLaw> laws(n - 1);
  ParameterizeResult result;
  result.dof = dof;
  result.segments.resize(n - 1);
  result.interpolation =
      "piecewise linear joint-space interpolation in arc length s, with a "
      "time-optimal trapezoidal (bang-coast-bang / triangular) path-speed "
      "profile per segment; zero speed at interior waypoints; zero-length "
      "segments are dwells";

  for (int k = 0; k < n - 1; ++k) {
    const Eigen::VectorXd dq = wp[k + 1] - wp[k];
    const double L = dq.norm();
    SegmentInfo& info = result.segments[k];
    info.index = k;
    info.length = L;
    info.duration = req.dwell_time;

    if (L <= kEps) {
      info.zero_length = true;
      info.binding_regime = "dwell";
      result.zero_length_segments.push_back(k);
      continue;
    }

    dir[k] = dq / L;
    // V_k = min over moving joints of v_max_j / |u_kj|  (velocity ceiling)
    // A_k = min over moving joints of a_max_j / |u_kj|  (acceleration ceiling)
    double V = std::numeric_limits<double>::infinity();
    double A = std::numeric_limits<double>::infinity();
    int jv = -1, ja = -1;
    for (int j = 0; j < dof; ++j) {
      const double uj = std::abs(dir[k][j]);
      if (uj <= kEps) continue;  // idle joint on this segment
      const double vj = req.velocity_limits[j] / uj;
      const double aj = req.acceleration_limits[j] / uj;
      if (vj < V) { V = vj; jv = j; }
      if (aj < A) { A = aj; ja = j; }
    }
    info.v_path_max = V;
    info.a_path_max = A;
    info.velocity_bottleneck_joint = jv;
    info.acceleration_bottleneck_joint = ja;
    laws[k].L = L;
    laws[k].A = A;
    laws[k].V = V;
  }

  // ----------------------------- requested boundary speeds (path coordinates)
  auto boundary_path_speed = [&](bool start) -> double {
    const Eigen::VectorXd& v = start ? req.start_velocity : req.end_velocity;
    const bool provided = start ? has_v0 : has_vn;
    if (!provided) return 0.0;
    const int kseg = start ? 0 : (n - 2);
    if (result.segments[kseg].zero_length) {
      Throw(422, "INVALID_BOUNDARY_VELOCITY",
          std::string(start ? "start" : "end") +
              "_velocity must be zero when the first/last segment has zero length");
    }
    const Eigen::VectorXd& u = dir[kseg];
    const double parallel = v.dot(u);
    const Eigen::VectorXd perp = v - parallel * u;
    const double scale = std::max(1.0, v.norm());
    if (perp.norm() > 1e-9 * scale || parallel < -1e-9 * scale) {
      Throw(422, "INVALID_BOUNDARY_VELOCITY",
          std::string(start ? "start" : "end") +
              "_velocity must be non-negative and parallel to the " +
              (start ? "first" : "last") + " segment direction");
    }
    return std::max(0.0, parallel);
  };

  const double s_start = boundary_path_speed(true);
  const double s_end = boundary_path_speed(false);

  // -------------------------------- per-segment time-optimal speed profiling
  for (int k = 0; k < n - 1; ++k) {
    SegmentInfo& info = result.segments[k];
    if (info.zero_length) {
      continue;  // dwell segment: duration already set to dwell_time
    }
    SegmentLaw& law = laws[k];
    const double s0 = (k == 0) ? s_start : 0.0;
    const double s1 = (k == n - 2) ? s_end : 0.0;
    law.s0 = s0;
    law.s1 = s1;
    const double L = law.L, A = law.A, V = law.V;

    // Ceiling check for requested boundary speeds.
    if (s0 > V * (1.0 + 1e-9) || s1 > V * (1.0 + 1e-9)) {
      Throw(422, "INFEASIBLE_BOUNDARY",
            "segment " + std::to_string(k) +
                " requested boundary speed exceeds the velocity ceiling "
                "(s0=" + std::to_string(s0) + ", s1=" + std::to_string(s1) +
                ", V=" + std::to_string(V) + ")");
    }

    // Minimum distance needed just to connect s0 -> s1 at acceleration limit.
    const double d_connect = std::abs(s1 * s1 - s0 * s0) / (2.0 * A);
    if (L < d_connect - 1e-9 * std::max(1.0, L)) {
      Throw(422, "INFEASIBLE_BOUNDARY",
            "segment " + std::to_string(k) +
                " is too short to honour the requested boundary speeds: "
                "available length " + std::to_string(L) +
                " < minimum required " + std::to_string(d_connect) +
                " (s0=" + std::to_string(s0) + ", s1=" + std::to_string(s1) +
                ", path acceleration limit A=" + std::to_string(A) + ")");
    }

    // Peak speed of the unconstrained bang-bang law:
    //   2 w^2 = 2 A L + s0^2 + s1^2
    const double w_tri =
        std::sqrt(std::max(0.0, A * L + 0.5 * (s0 * s0 + s1 * s1)));

    if (w_tri < V * (1.0 - 1e-10)) {
      // Triangular profile: acceleration limit alone shapes the segment.
      law.w = w_tri;
      law.velocity_bound = false;
      law.t_acc = std::max(0.0, (w_tri - s0) / A);
      law.t_dec = std::max(0.0, (w_tri - s1) / A);
      law.d_acc = (w_tri * w_tri - s0 * s0) / (2.0 * A);
      law.d_cruise = 0.0;
    } else {
      // Trapezoidal profile: cruise at the velocity ceiling V.
      law.w = V;
      law.velocity_bound = true;
      law.t_acc = std::max(0.0, (V - s0) / A);
      law.t_dec = std::max(0.0, (V - s1) / A);
      law.d_acc = (V * V - s0 * s0) / (2.0 * A);
      const double d_dec = (V * V - s1 * s1) / (2.0 * A);
      law.d_cruise = std::max(0.0, L - law.d_acc - d_dec);
      law.t_cruise = law.d_cruise / V;
    }

    info.duration = law.duration();
    info.peak_path_speed = law.w;
    info.binding_regime = law.velocity_bound ? "velocity" : "acceleration";
    info.bottleneck_velocity_ratio = law.w / V;
    info.bottleneck_accel_ratio =
        (law.t_acc + law.t_dec > 0.0) ? 1.0 : 0.0;

    if (!(info.duration > 0.0) || !std::isfinite(info.duration)) {
      Throw(500, "NUMERIC_FAILURE",
            "segment " + std::to_string(k) + " produced a non-positive duration");
    }
  }

  // -------------------------------------------------------------- time states
  result.states.resize(n);
  double t = 0.0;
  for (int i = 0; i < n; ++i) {
    WaypointState& st = result.states[i];
    st.time = t;
    st.position = wp[i];
    st.velocity = Eigen::VectorXd::Zero(dof);
    st.acceleration = Eigen::VectorXd::Zero(dof);
    if (i < n - 1) {
      const SegmentInfo& info = result.segments[i];
      if (!info.zero_length) {
        st.acceleration = laws[i].A * dir[i];  // leaving the waypoint: +A phase
      }
      t += info.duration;
    }
  }
  // Boundary velocities (interior waypoints are full stops, by construction).
  if (has_v0) result.states[0].velocity = req.start_velocity;
  if (has_vn) result.states[n - 1].velocity = req.end_velocity;

  // ----------------------------- dense sampling of the CONTINUOUS time law
  VerificationReport& ver = result.verification;
  ver.samples_per_segment = req.samples_per_segment;
  ver.velocity_tolerance = 1e-9 * std::max(1.0, req.velocity_limits.maxCoeff());
  ver.acceleration_tolerance =
      1e-9 * std::max(1.0, req.acceleration_limits.maxCoeff());
  ver.max_velocity_violation = 0.0;
  ver.max_acceleration_violation = 0.0;
  ver.time_min_gap = std::numeric_limits<double>::infinity();

  int total_samples = 0;
  for (int k = 0; k < n - 1; ++k) {
    const SegmentInfo& info = result.segments[k];
    const int N = req.samples_per_segment;
    const double T = info.duration;
    double t0 = 0.0;
    for (int i = 0; i < k; ++i) t0 += result.segments[i].duration;

    for (int m = 0; m < N; ++m) {
      const double tau = T * static_cast<double>(m) / (N - 1);
      Eigen::VectorXd q, v, a;
      if (info.zero_length) {
        q = wp[k];
        v = Eigen::VectorXd::Zero(dof);
        a = Eigen::VectorXd::Zero(dof);
      } else {
        const SegmentLaw& law = laws[k];
        double s, sd, sdd;
        law.eval(std::min(tau, T), s, sd, sdd);
        q = wp[k] + s * dir[k];
        v = sd * dir[k];
        a = sdd * dir[k];
      }
      // Boundary samples coincide with waypoint states; velocities there may
      // be the requested start/end speeds, which are themselves checked.
      for (int j = 0; j < dof; ++j) {
        ver.max_velocity_violation =
            std::max(ver.max_velocity_violation,
                     std::abs(v[j]) - req.velocity_limits[j]);
        ver.max_acceleration_violation =
            std::max(ver.max_acceleration_violation,
                     std::abs(a[j]) - req.acceleration_limits[j]);
      }
      ++total_samples;
    }
    ver.time_min_gap = std::min(ver.time_min_gap, T / (N - 1));
  }

  const Eigen::VectorXd v0check = has_v0 ? req.start_velocity
                                        : Eigen::VectorXd::Zero(dof);
  const Eigen::VectorXd vncheck = has_vn ? req.end_velocity
                                         : Eigen::VectorXd::Zero(dof);
  for (int j = 0; j < dof; ++j) {
    ver.max_velocity_violation =
        std::max(ver.max_velocity_violation,
                 std::max(std::abs(v0check[j]), std::abs(vncheck[j])) -
                     req.velocity_limits[j]);
  }

  // Identify worst joints from the segment ceilings (the joints that can
  // actually bind); scan sample violations too for an exact report.
  ver.worst_velocity_joint = 0;
  ver.worst_acceleration_joint = 0;
  {
    int jv = 0, ja = 0;
    double bv = -std::numeric_limits<double>::infinity();
    double ba = -std::numeric_limits<double>::infinity();
    for (int j = 0; j < dof; ++j) {
      // Global worst utilization from per-segment bottleneck data:
      for (const auto& s : result.segments) {
        if (s.velocity_bottleneck_joint == j) {
          if (s.bottleneck_velocity_ratio > bv) {
            bv = s.bottleneck_velocity_ratio; jv = j;
          }
        }
        if (s.acceleration_bottleneck_joint == j &&
            s.binding_regime == "acceleration") {
          if (s.bottleneck_accel_ratio > ba) {
            ba = s.bottleneck_accel_ratio; ja = j;
          }
        }
      }
    }
    ver.worst_velocity_joint = jv;
    ver.worst_acceleration_joint = ja;
  }

  ver.total_samples = total_samples;
  ver.passed =
      ver.max_velocity_violation <= ver.velocity_tolerance &&
      ver.max_acceleration_violation <= ver.acceleration_tolerance;

  // Strictly-increasing time check (waypoint level; dwell segments count).
  for (int k = 0; k < n - 1; ++k) {
    ver.time_min_gap =
        std::min(ver.time_min_gap, result.states[k + 1].time -
                                       result.states[k].time);
  }
  if (!(ver.time_min_gap > 0.0)) ver.passed = false;

  return result;
}

}  // namespace jtp
