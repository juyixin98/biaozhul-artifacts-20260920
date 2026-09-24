#pragma once
#include <Eigen/Dense>

namespace topp {

// Natural cubic spline through the waypoints with UNIFORM knot spacing h=1 in
// the path coordinate u. Each joint is an independent 1-D spline S_j(u),
// u in [0, n-1]. Natural end conditions: S''(0) = S''(n-1) = 0.
//
// On segment u in [i, i+1], with d_i = S''(u_i):
//   S(u) = (1-x) q_i + x q_{i+1} + ((1-x)^3-(1-x)) d_i/6
//                                 + (x^3-x) d_{i+1}/6,  x = u - i
// which is C2 at the knots: velocities and accelerations are continuous.
class NaturalCubicSpline {
public:
    explicit NaturalCubicSpline(const Eigen::MatrixXd& waypoints);

    int dof() const { return dof_; }
    double uMax() const { return static_cast<double>(segments_); }

    // S(u), S'(u), S''(u). u may be anywhere in [0, umax].
    Eigen::VectorXd value(double u) const;
    Eigen::VectorXd derivative(double u) const;
    Eigen::VectorXd secondDerivative(double u) const;

    // Values at an integer knot (used for the reported waypoint states).
    Eigen::VectorXd knotValue(int i) const { return q_.col(i); }
    Eigen::VectorXd knotDerivative(int i) const;
    Eigen::VectorXd knotSecondDerivative(int i) const { return d_.col(i); }

private:
    int dof_;
    int segments_;          // number of segments = knots - 1
    Eigen::MatrixXd q_;     // dof x (segments+1)
    Eigen::MatrixXd d_;     // dof x (segments+1), second derivatives at knots
};

} // namespace topp
