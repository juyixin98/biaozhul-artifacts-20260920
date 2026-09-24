#include "topp/parameterize.hpp"
#include "topp/types.hpp"
#include <algorithm>
#include <cmath>
#include <limits>
#include <sstream>

namespace topp {

namespace {
constexpr double INF = std::numeric_limits<double>::infinity();
constexpr double DERIV_EPS = 1e-10;   // below this |S'| an axis is stationary

// Pairwise velocity-acceleration MVC (Pham 2014).
// For axes i != j the intersection of their acceleration strips requires
//   min_{si in [-ai,ai]} (s_j - (pp_i*p_j/p_i - pp_j) x) - p_j/p_i s_i <= a_j
// which reduces (let r = p_j/p_i, c = r*pp_i - pp_j, a_i = am_i, a_j = am_j)
// to  -ai|r| - c x - a_j <= 0, i.e.
//   c > 0 : x <= (a_j + ai|r|) / c
// Each ordering (i,j) and (j,i) is evaluated in the main loop.
} // namespace

RawProfile parameterizeTimeOptimal(const NaturalCubicSpline& spline,
                                   const Limits& lim,
                                   int gridCells,
                                   double x0,
                                   int substeps) {
    const int dof = spline.dof();
    const double umax = spline.uMax();
    substeps = std::max(1, substeps);
    const int N = gridCells * substeps;   // internal (refined) grid
    const double ds = umax / N;

    std::vector<double> sGrid(N + 1), mvc(N + 1);
    std::vector<std::string> reason(N + 1);
    for (int k = 0; k <= N; ++k) sGrid[k] = std::min(k * ds, umax);

    // ---- Maximum velocity curve ------------------------------------------
    for (int k = 0; k <= N; ++k) {
        double u = sGrid[k];
        Eigen::VectorXd p = spline.derivative(u);
        Eigen::VectorXd pp = spline.secondDerivative(u);

        double bound = INF;
        std::string why;

        for (int i = 0; i < dof; ++i) {
            if (std::abs(p(i)) > DERIV_EPS) {
                // |p| sqrt(x) <= v  ->  x <= (v/|p|)^2
                double mv = lim.vMax(i) / std::abs(p(i));
                double xb_v = mv * mv;
                if (xb_v < bound) { bound = xb_v; why = "v:" + std::to_string(i); }
            }
            // A joint moving tangentially slowly still has the path-geometry
            // ("centripetal") acceleration  pp*x = S''(u) sd^2, which must
            // satisfy |..| <= am even when p -> 0 (joint turns over at a
            // cusp): x <= am/|pp|. Essential for joints whose tangent is zero.
            if (std::abs(pp(i)) > 1e-12) {
                double xb_a = lim.aMax(i) / std::abs(pp(i));
                if (xb_a < bound) { bound = xb_a; why = "a0:" + std::to_string(i); }
            }
        }
        // Pairwise acceleration constraints (both orderings, take minimum).
        for (int i = 0; i < dof; ++i) {
            if (std::abs(p(i)) <= DERIV_EPS) continue;
            for (int j = i + 1; j < dof; ++j) {
                if (std::abs(p(j)) <= DERIV_EPS) continue;
                double r = p(j) / p(i);
                double cPos = r * pp(i) - pp(j);
                double num = lim.aMax(j) + lim.aMax(i) * std::abs(r);
                if (cPos > 1e-12) {
                    double xb = num / cPos;
                    if (xb < bound) {
                        bound = xb;
                        why = "a:" + std::to_string(i) + ":" + std::to_string(j);
                    }
                }
                // swapped i<->j
                double r2 = p(i) / p(j);
                double cNeg = r2 * pp(j) - pp(i);
                double num2 = lim.aMax(i) + lim.aMax(j) * std::abs(r2);
                if (cNeg > 1e-12) {
                    double xb = num2 / cNeg;
                    if (xb < bound) {
                        bound = xb;
                        why = "a:" + std::to_string(j) + ":" + std::to_string(i);
                    }
                }
            }
        }

        mvc[k] = std::max(0.0, bound);
        reason[k] = (bound == INF ? "" : why);
    }

    // Boundary start speed feasibility.
    if (x0 > mvc[0] * (1.0 + 1e-9) + 1e-12) {
        std::ostringstream oss;
        oss << "requested start speed exceeds the feasible maximum at the "
               "first point (start x=" << x0 << ", MVC x=" << mvc[0] << ")";
        throw ApiError(ErrorCode::INFEASIBLE_BOUNDARY, oss.str());
    }
    double xStart = std::min(std::max(0.0, x0), mvc[0]);

    // Cache path derivatives at every grid node (used by both passes).
    std::vector<Eigen::VectorXd> P(N+1), PP(N+1);
    for (int k = 0; k <= N; ++k) {
        double u = sGrid[k];
        P[k]  = spline.derivative(u);
        PP[k] = spline.secondDerivative(u);
    }

    // Per-cell feasible sdd interval for the CONSTANT control model.
    // Within a cell x(u) = x_ref + 2 (u-u_ref) sdd, hence at offset h in
    // path coordinate from the known endpoint:
    //   forward (known x_k) : x = x_k + 2 h sdd
    //   backward(known x_{k+1}): x = x_{k+1} - 2 (ds-h) sdd, h in [0,ds].
    // We intersect, at several in-cell sample offsets, EVERY joint's
    //   |p(u) sdd + pp(u) x(u)| <= aMax, and the speed bound x(u) <= MVC(u).
    // Sampling inside the cell (not just endpoints) closes the gap that lets
    // a locally tightening geometry bound (2/|S''|, whose required sdd is a
    // third-derivative term) be crossed by a piecewise-linear MVC.
    const int QSUB = 10;  // in-cell constraint samples (incl. both ends)
    struct Lim { double lo, hi; };
    auto intersectSpeed = [&](double x0, double coef,
                              double mv, Lim& iv) {
        // x0 + coef*sdd <= mv  and  >= 0
        if (coef > 1e-15) {
            iv.hi = std::min(iv.hi, (mv - x0)/coef);
            iv.lo = std::max(iv.lo, -x0/coef);
        } else if (coef < -1e-15) {
            iv.lo = std::max(iv.lo, (mv - x0)/coef);
            iv.hi = std::min(iv.hi, -x0/coef);
        } else {
            if (x0 > mv + 1e-9) { iv.lo = 1.0; iv.hi = 0.0; }
        }
    };

    // ---- Backward pass ----------------------------------------------------
    std::vector<double> xb(N + 1, 0.0);
    xb[N] = 0.0;  // end at rest
    for (int k = N - 1; k >= 0; --k) {
        Lim iv{-INF, INF};
        for (int q = 0; q <= QSUB; ++q) {
            double h = ds * q / QSUB;            // 0 at node k+1 -> ds at k
            double u = sGrid[k] + h;
            // coefficient of sdd in x(u): x = x_{k+1} - 2(ds-h) sdd
            double coefX = -2.0*(ds - h);
            double xv = xb[k+1];  // accel constraint rewritten below directly
            // |p sdd + pp (x_{k+1} - 2(ds-h) sdd)| <= a:
            //   (p - 2(ds-h) pp) sdd ... + pp x_{k+1}
            Eigen::VectorXd pv = spline.derivative(u);
            Eigen::VectorXd qv = spline.secondDerivative(u);
            for (int j = 0; j < dof; ++j) {
                double den = pv(j) + coefX*qv(j);
                double c = qv(j)*xv;
                double l = -lim.aMax(j) - c, uB = lim.aMax(j) - c;
                if (std::abs(den) <= 1e-15) {
                    if (!(c <= lim.aMax(j)+1e-9)) { iv.lo=1.0; iv.hi=0.0; }
                } else if (den > 0) {
                    iv.lo=std::max(iv.lo,l/den); iv.hi=std::min(iv.hi,uB/den);
                } else {
                    iv.lo=std::max(iv.lo,uB/den); iv.hi=std::min(iv.hi,l/den);
                }
            }
            // speed bound: x(u) = x_{k+1} - 2(ds-h) sdd <= MVC at this offset
            double mv = mvc[k] + (mvc[k+1]-mvc[k])*(h/ds);
            intersectSpeed(xb[k+1], coefX, mv, iv);
        }
        // Pick the admissible constant control that maximizes reachable x_k.
        // x_k = x_{k+1} - 2 ds sdd, so the LARGEST x_k comes from the SMALLEST
        // (most negative) admissible sdd.
        double sdd = std::isfinite(iv.lo) ? iv.lo : iv.hi;
        double xk = xb[k+1] - 2.0*ds*sdd;
        if (xk > mvc[k] + 1e-11) {
            // Braking within the cell cannot keep us under MVC_k: ride the
            // MVC instead, choosing the in-interval control closest to it.
            double target = (xb[k+1] - mvc[k]) / (2.0*ds);
            if (std::isfinite(iv.lo) && std::isfinite(iv.hi))
                sdd = std::clamp(target, iv.lo, iv.hi);
            xk = xb[k+1] - 2.0*ds*sdd;
            if (xk > mvc[k]) { sdd = (xb[k+1]-mvc[k])/(2.0*ds); xk = mvc[k]; }
        }
        xb[k] = std::clamp(xk, 0.0, mvc[k]);
    }

    // ---- Forward pass -----------------------------------------------------
    // sddFwd[k] is the GROUND-TRUTH constant control applied on cell k; the
    // endpoint speed x_{k+1} is derived from it (never recomputed elsewhere),
    // so the stored profile and the verification use the identical control.
    std::vector<double> xf(N + 1, 0.0), sddFwd(N, 0.0);
    xf[0] = std::min(xStart, xb[0]);
    for (int k = 0; k < N; ++k) {
        Lim iv{-INF, INF};
        for (int q = 0; q <= QSUB; ++q) {
            double h = ds * q / QSUB;            // 0 at node k -> ds at k+1
            double u = sGrid[k] + h;
            double coefX = 2.0*h;
            Eigen::VectorXd pv = spline.derivative(u);
            Eigen::VectorXd qv = spline.secondDerivative(u);
            for (int j = 0; j < dof; ++j) {
                double den = pv(j) + coefX*qv(j);
                double c = qv(j)*xf[k];
                double l = -lim.aMax(j) - c, uB = lim.aMax(j) - c;
                if (std::abs(den) <= 1e-15) {
                    if (!(c <= lim.aMax(j)+1e-9)) { iv.lo=1.0; iv.hi=0.0; }
                } else if (den > 0) {
                    iv.lo=std::max(iv.lo,l/den); iv.hi=std::min(iv.hi,uB/den);
                } else {
                    iv.lo=std::max(iv.lo,uB/den); iv.hi=std::min(iv.hi,l/den);
                }
            }
            double mv = mvc[k] + (mvc[k+1]-mvc[k])*(h/ds);
            intersectSpeed(xf[k], coefX, mv, iv);
        }
        double cap = std::min(xb[k+1], mvc[k+1]);
        double sdd = std::isfinite(iv.hi) ? iv.hi : iv.lo;   // max accel
        double xnext = xf[k] + 2.0*ds*sdd;
        if (!std::isfinite(xnext) || xnext < 0.0 || xnext > cap) {
            // Join the reachable curve: pick the admissible control whose
            // endpoint lands closest to (without exceeding) the cap.
            double target = (cap - xf[k]) / (2.0*ds);
            if (std::isfinite(iv.lo) && std::isfinite(iv.hi))
                sdd = std::clamp(target, iv.lo, iv.hi);
            xnext = xf[k] + 2.0*ds*sdd;
            if (xnext < 0.0) { sdd = -xf[k]/(2.0*ds); xnext = 0.0; }
            if (xnext > cap)  { sdd = (cap-xf[k])/(2.0*ds); xnext = cap; }
        }
        sddFwd[k] = sdd;
        xf[k+1] = xnext;
    }



    // ---- Time integration (constant control model) ------------------------
    std::vector<double> times(N + 1, 0.0);
    for (int k = 0; k < N; ++k) {
        double xA = xf[k], xB = xf[k + 1];
        double dt;
        double sqrtA = std::sqrt(std::max(0.0, xA));
        double sqrtB = std::sqrt(std::max(0.0, xB));
        if (sqrtA + sqrtB > 1e-12) {
            dt = 2.0 * ds / (sqrtA + sqrtB);
        } else {
            // Zero speed over a non-zero path would mean a stall.
            if (ds > 1e-15)
                throw ApiError(ErrorCode::INFEASIBLE_PROFILE,
                               "zero-speed stall detected during time integration");
            dt = 0.0;
        }
        times[k + 1] = times[k] + dt;
    }

    RawProfile out;
    out.N = N;
    out.ds = ds;
    out.sGrid = std::move(sGrid);
    out.x = std::move(xf);
    out.sdd = std::move(sddFwd);
    out.times = std::move(times);
    out.mvcReason = std::move(reason);
    return out;
}

} // namespace topp
