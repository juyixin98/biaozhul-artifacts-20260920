#pragma once
#include "topp/spline.hpp"
#include <vector>

namespace topp {

struct RawProfile {
    int N;                       // number of grid cells
    double ds;                   // grid spacing in path coordinate
    std::vector<double> sGrid;   // N+1 grid positions
    std::vector<double> x;       // N+1 tangent speeds squared x = sd^2
    std::vector<double> sdd;     // N tangent accelerations per cell start
    std::vector<double> times;   // N+1 cumulative times
    // Per-grid-cell dominant constraint bookkeeping.
    // "v:<joint>" velocity MVC, "a:<joint>:<other>" acceleration MVC, "dyn", ""
    std::vector<std::string> mvcReason;
};

struct Limits {
    Eigen::VectorXd vMax;
    Eigen::VectorXd aMax;
};

// Time-optimal time parameterization of a geometric path under box velocity
// and acceleration constraints (double-integrator joints).
//
// Interpolation: natural cubic spline (C2), see spline.hpp.
// Method: Pham (2014) "General formulation of the feed-forward interpolation
// and time-optimization algorithms" — maximum-velocity curve from pairwise
// acceleration polytope + velocity bounds, then backward and forward passes
// with implicit Euler integration. This makes ALL axes satisfy their limits
// simultaneously and includes acceleration coupling across segments
// (a pure per-segment vmax/amax trapezoid split would not).
//
// x0 = start tangent speed squared (0 = start from rest).
// substeps: internal grid refinement factor. The MVC/alpha/beta coefficients
// vary within a user cell; integrating them on a refined grid bounds the
// in-cell (continuous) constraint overshoot to first-order accuracy.
RawProfile parameterizeTimeOptimal(const NaturalCubicSpline& spline,
                                   const Limits& limits,
                                   int gridCells,
                                   double x0,
                                   int substeps = 6);

} // namespace topp
