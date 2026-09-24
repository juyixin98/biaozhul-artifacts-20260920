#include "topp/spline.hpp"
#include <algorithm>
#include <cmath>

namespace topp {

NaturalCubicSpline::NaturalCubicSpline(const Eigen::MatrixXd& waypoints)
    : dof_(static_cast<int>(waypoints.rows())),
      segments_(static_cast<int>(waypoints.cols()) - 1),
      q_(waypoints),
      d_(Eigen::MatrixXd::Zero(dof_, waypoints.cols())) {
    if (waypoints.cols() < 2)
        throw std::invalid_argument("need at least 2 waypoints");

    const int m = segments_ - 1;  // interior knots count
    if (m <= 0) return;           // 2 knots: straight line

    // Tridiagonal system for interior second derivatives D (natural ends):
    //   D[i-1] + 4 D[i] + D[i+1] = 6(q[i-1] - 2 q[i] + q[i+1]),
    // indices 1..segments_-1. Solve per joint with the Thomas algorithm.
    std::vector<double> cp(m), dp(m);
    for (int j = 0; j < dof_; ++j) {
        auto rhsAt = [&](int interior) {
            int i = interior + 1;
            return 6.0 * (q_(j, i - 1) - 2.0 * q_(j, i) + q_(j, i + 1));
        };
        cp[0] = 1.0 / 4.0;
        dp[0] = rhsAt(0) / 4.0;
        for (int k = 1; k < m; ++k) {
            double denom = 4.0 - cp[k - 1];
            if (k < m - 1) cp[k] = 1.0 / denom;
            dp[k] = (rhsAt(k) - dp[k - 1]) / denom;
        }
        std::vector<double> x(m);
        x[m - 1] = dp[m - 1];
        for (int k = m - 2; k >= 0; --k)
            x[k] = dp[k] - cp[k] * x[k + 1];
        for (int k = 0; k < m; ++k)
            d_(j, k + 1) = x[k];
    }
}

namespace {
struct SegPoint {
    int i;
    double x;
};
SegPoint locate(double u, int segments) {
    int i = static_cast<int>(std::floor(u));
    i = std::clamp(i, 0, segments - 1);
    double x = std::clamp(u - i, 0.0, 1.0);
    return {i, x};
}
} // namespace

Eigen::VectorXd NaturalCubicSpline::value(double u) const {
    auto [i, x] = locate(u, segments_);
    double a = 1.0 - x;
    const auto& qi = q_.col(i);
    const auto& qn = q_.col(i + 1);
    const auto& di = d_.col(i);
    const auto& dn = d_.col(i + 1);
    return a * qi + x * qn +
           ((std::pow(a, 3) - a) * di + (std::pow(x, 3) - x) * dn) / 6.0;
}

Eigen::VectorXd NaturalCubicSpline::derivative(double u) const {
    auto [i, x] = locate(u, segments_);
    double a = 1.0 - x;
    const auto& qi = q_.col(i);
    const auto& qn = q_.col(i + 1);
    const auto& di = d_.col(i);
    const auto& dn = d_.col(i + 1);
    return qn - qi +
           (1.0 - 3.0 * a * a) * di / 6.0 +
           (3.0 * x * x - 1.0) * dn / 6.0;
}

Eigen::VectorXd NaturalCubicSpline::secondDerivative(double u) const {
    auto [i, x] = locate(u, segments_);
    double a = 1.0 - x;
    return a * d_.col(i) + x * d_.col(i + 1);
}

Eigen::VectorXd NaturalCubicSpline::knotDerivative(int i) const {
    // Segment i starts at knot i with x=0: S' = dq - d_i/3 - d_{i+1}/6.
    // At the final knot use the preceding segment at x=1: dq + d_{i-1}/6 + d_i/3.
    if (i >= segments_) {
        return q_.col(i) - q_.col(i - 1) +
               d_.col(i - 1) / 6.0 + d_.col(i) / 3.0;
    }
    return q_.col(i + 1) - q_.col(i) -
           d_.col(i) / 3.0 - d_.col(i + 1) / 6.0;
}

} // namespace topp
