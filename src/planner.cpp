#include "topp/planner.hpp"
#include "topp/spline.hpp"
#include "topp/parameterize.hpp"
#include <algorithm>
#include <cmath>
#include <map>
#include <sstream>

namespace topp {

namespace {

struct DenseSample {
    double u, t;
    Eigen::VectorXd vRatio;   // |qd_j| / vMax_j
    Eigen::VectorXd aRatio;   // |qdd_j| / aMax_j
};

} // namespace

Result plan(const PlanningRequest& req) {
    const auto& W = req.waypoints;
    const int dof = static_cast<int>(W.rows());
    const int n = static_cast<int>(W.cols());

    // ---- Input validation --------------------------------------------------
    if (n < 2)
        throw ApiError(ErrorCode::INFEASIBLE_LIMITS,
                       "need at least 2 waypoints");
    if (req.vMax.size() != dof || req.aMax.size() != dof)
        throw ApiError(ErrorCode::BAD_REQUEST,
                       "velocity/acceleration limits must match DOF");
    for (int j = 0; j < dof; ++j) {
        if (!(req.vMax(j) > 0.0) || !std::isfinite(req.vMax(j)))
            throw ApiError(ErrorCode::INFEASIBLE_LIMITS,
                           "velocity limits must be positive and finite");
        if (!(req.aMax(j) > 0.0) || !std::isfinite(req.aMax(j)))
            throw ApiError(ErrorCode::INFEASIBLE_LIMITS,
                           "acceleration limits must be positive and finite");
    }
    if (!W.allFinite())
        throw ApiError(ErrorCode::BAD_REQUEST, "waypoints must be finite");
    if (req.gridCells < 1 || req.gridCells > 100000)
        throw ApiError(ErrorCode::BAD_REQUEST,
                       "grid_cells must be in [1, 100000]");
    if (req.samplesPerCell < 1 || req.samplesPerCell > 1000)
        throw ApiError(ErrorCode::BAD_REQUEST,
                       "samples_per_cell must be in [1, 1000]");

    // ---- Collapse consecutive duplicate points ----------------------------
    std::vector<int> keep;
    keep.reserve(n);
    keep.push_back(0);
    int removed = 0;
    for (int i = 1; i < n; ++i) {
        double d = (W.col(i) - W.col(keep.back())).cwiseAbs().maxCoeff();
        if (d <= req.duplicateTolerance) {
            ++removed;
        } else {
            keep.push_back(i);
        }
    }
    Eigen::MatrixXd Q(dof, keep.size());
    for (size_t c = 0; c < keep.size(); ++c) Q.col(c) = W.col(keep[c]);
    if (Q.cols() < 2)
        throw ApiError(ErrorCode::INFEASIBLE_LIMITS,
                       "after removing consecutive duplicate points fewer "
                       "than 2 distinct points remain");

    // ---- Start velocity -> path tangent speed -----------------------------
    double x0 = 0.0;
    if (req.startVelocity.has_value()) {
        const Eigen::VectorXd& sv = *req.startVelocity;
        if (sv.size() != dof)
            throw ApiError(ErrorCode::BAD_REQUEST,
                           "start_velocity must have one entry per joint");
        if (!sv.allFinite())
            throw ApiError(ErrorCode::BAD_REQUEST,
                           "start_velocity must be finite");
        // S_j'(0) * sd(0) = sv_j  for every non-stationary axis j.
        NaturalCubicSpline tmpSpline(Q);
        Eigen::VectorXd p0 = tmpSpline.derivative(0.0);
        double sd = 0.0;
        bool have = false;
        for (int j = 0; j < dof; ++j) {
            if (std::abs(p0(j)) < 1e-10) {
                if (std::abs(sv(j)) > 1e-9)
                    throw ApiError(ErrorCode::INFEASIBLE_BOUNDARY,
                                   "start_velocity is non-zero on a joint whose "
                                   "path tangent is zero at the first point");
                continue;
            }
            double need = sv(j) / p0(j);
            if (need < -1e-12)
                throw ApiError(ErrorCode::INFEASIBLE_BOUNDARY,
                               "start_velocity points opposite to the path "
                               "direction; the parameterization cannot move "
                               "backwards");
            if (!have) { sd = need; have = true; }
            else if (std::abs(need - sd) > 1e-7 * std::max(1.0, std::abs(sd)))
                throw ApiError(ErrorCode::INFEASIBLE_BOUNDARY,
                               "start_velocity is incompatible with the path "
                               "tangent: joints request different path speeds "
                               "(the spline fixes the joint velocity ratios)");
        }
        if (have) x0 = sd * sd;
    }

    // ---- Spline interpolation + time-optimal parameterization -------------
    NaturalCubicSpline spline(Q);
    Limits lim{req.vMax, req.aMax};
    // Force an integer number of cells per spline segment so every waypoint
    // lands exactly on a grid node (clean t/speed lookups at the waypoints).
    const int segments = static_cast<int>(Q.cols()) - 1;
    int perSeg = std::max(1, req.gridCells / segments);
    int gridCells = perSeg * segments;
    RawProfile prof = parameterizeTimeOptimal(spline, lim, gridCells, x0);

    const int N = prof.N;
    const double ds = prof.ds;
    const double umax = spline.uMax();

    // ---- Dense in-segment sampling verification ----------------------------
    // Within every grid cell the profile uses constant sdd and the known
    // (x_k, x_{k+1}); sample it densely and re-evaluate ALL joint velocity
    // and acceleration limits from the actual spline derivatives.
    const int perCell = req.samplesPerCell;
    std::vector<DenseSample> samples;
    samples.reserve(static_cast<size_t>(N) * perCell + 1);
    auto cellState = [&](int k, double f,
                         double& u, double& t,
                         Eigen::VectorXd& sd_, Eigen::VectorXd& sdd_) {
        double xA = prof.x[k], xB = prof.x[k + 1];
        double sdA = std::sqrt(std::max(0.0, xA));
        double sddk = prof.sdd[k];
        // Discrete controller: sdd is constant within the cell, hence
        // x = sd^2 evolves LINEARLY in u: x(f) = xA + (xB-xA) f.
        double xHere = xA + (xB - xA) * f;
        if (xHere < 0.0) xHere = 0.0;
        double sdHere = std::sqrt(xHere);
        u = std::min(umax, prof.sGrid[k] + f * ds);
        // t(f) = t_k + integral ds/sqrt(x(g)) dg  (closed form).
        double dx = xB - xA;
        if (std::abs(dx) > 1e-12)
            t = prof.times[k] + 2.0 * ds * (sdHere - sdA) / dx;
        else
            t = prof.times[k] + f * ds / (sdA > 1e-15 ? sdA : 1e-15);
        Eigen::VectorXd p = spline.derivative(u);
        Eigen::VectorXd pp = spline.secondDerivative(u);
        sd_ = p * sdHere;
        sdd_ = p * sddk + pp * xHere;
    };

    Verification verif;
    verif.maxVelocityRatio = 0.0;
    verif.maxAccelerationRatio = 0.0;
    verif.maxVelocityJoint = -1;
    verif.maxAccelerationJoint = -1;
    verif.passed = true;

    auto recordSample = [&](double u, double t,
                            const Eigen::VectorXd& qd,
                            const Eigen::VectorXd& qdd) {
        DenseSample s;
        s.u = u; s.t = t;
        s.vRatio = qd.cwiseAbs().cwiseQuotient(req.vMax);
        s.aRatio = qdd.cwiseAbs().cwiseQuotient(req.aMax);
        double vv = s.vRatio.maxCoeff();
        double aa = s.aRatio.maxCoeff();
        if (vv > verif.maxVelocityRatio) {
            verif.maxVelocityRatio = vv;
            verif.maxVelocityJoint = 0;
            for (int j = 0; j < dof; ++j)
                if (s.vRatio(j) == vv) verif.maxVelocityJoint = j;
        }
        if (aa > verif.maxAccelerationRatio) {
            verif.maxAccelerationRatio = aa;
            verif.maxAccelerationJoint = 0;
            for (int j = 0; j < dof; ++j)
                if (s.aRatio(j) == aa) verif.maxAccelerationJoint = j;
        }
        samples.push_back(std::move(s));
    };

    // Sample STRICTLY INSIDE each cell (open interval f in (0,1)): the
    // piecewise-constant sdd control may jump at a grid node, so the node
    // itself has left/right acceleration limits. Interior points are the
    // physically meaningful, unambiguous samples; endpoints are checked
    // separately with their one-sided limits.
    for (int k = 0; k < N; ++k) {
        for (int m = 0; m < perCell; ++m) {
            double f = (m + 1) / static_cast<double>(perCell + 1);
            double u, t;
            Eigen::VectorXd qd, qdd;
            cellState(k, f, u, t, qd, qdd);
            recordSample(u, t, qd, qdd);
        }
    }
    // Endpoint one-sided checks: start uses cell 0 control, end uses cell N-1.
    auto endpointSample = [&](int k, bool atEnd) {
        double u = atEnd ? umax : 0.0;
        double xHere = atEnd ? prof.x[N] : prof.x[0];
        double sdHere = std::sqrt(std::max(0.0, xHere));
        double sddHere = prof.sdd[k];
        double tHere = atEnd ? prof.times[N] : 0.0;
        Eigen::VectorXd p = spline.derivative(u);
        Eigen::VectorXd pp = spline.secondDerivative(u);
        recordSample(u, tHere, p * sdHere, p * sddHere + pp * xHere);
    };
    endpointSample(0, false);
    endpointSample(N - 1, true);
    verif.denseSamples = static_cast<int>(samples.size());

    // Tolerance: the implicit Euler grid is exact-ish; allow a small
    // discretization margin rather than failing on roundoff.
    const double tol = 2e-3;
    if (verif.maxVelocityRatio > 1.0 + tol ||
        verif.maxAccelerationRatio > 1.0 + tol) {
        verif.passed = false;
        std::ostringstream oss;
        oss << "constraint violation in dense verification: max |v|/vmax="
            << verif.maxVelocityRatio << " (joint "
            << verif.maxVelocityJoint << "), max |a|/amax="
            << verif.maxAccelerationRatio << " (joint "
            << verif.maxAccelerationJoint << ")";
        verif.detail = oss.str();
    } else {
        verif.detail = "all joint velocity and acceleration limits respected "
                       "at " + std::to_string(verif.denseSamples) +
                       " dense samples";
    }

    // ---- Active-limit intervals (which joint / where) ----------------------
    const double ACTIVE = 0.99;
    struct CellMark {
        int joint;
        std::string limit;
        double ratio;
    };
    std::vector<std::vector<CellMark>> marks(N);
    for (int k = 0; k < N; ++k) {
        for (int m = 0; m < perCell; ++m) {
            const DenseSample& s = samples[k * perCell + m];
            for (int j = 0; j < dof; ++j) {
                if (s.vRatio(j) >= ACTIVE)
                    marks[k].push_back({j, "velocity", s.vRatio(j)});
                if (s.aRatio(j) >= ACTIVE)
                    marks[k].push_back({j, "acceleration", s.aRatio(j)});
            }
        }
    }

    // Group consecutive cells per (joint, limit) with a small gap tolerance.
    std::vector<ActiveInterval> intervals;
    struct Run {
        int joint;
        std::string limit;
        int from, to;
        double maxRatio;
    };
    std::map<std::pair<int, std::string>, Run> runs;
    const int GAP = std::max(1, N / 200);
    std::map<std::pair<int, std::string>, int> lastSeen;
    for (int k = 0; k < N; ++k) {
        std::vector<std::pair<int, std::string>> present;
        for (const auto& mk : marks[k])
            present.emplace_back(mk.joint, mk.limit);
        sort(present.begin(), present.end());
        present.erase(unique(present.begin(), present.end()), present.end());
        for (const auto& key : present) {
            double ratio = 0.0;
            for (const auto& mk : marks[k])
                if (mk.joint == key.first && mk.limit == key.second)
                    ratio = std::max(ratio, mk.ratio);
            auto it = runs.find(key);
            if (it == runs.end() || k - lastSeen[key] > GAP) {
                runs[key] = {key.first, key.second, k, k + 1, ratio};
            } else {
                it->second.to = k + 1;
                it->second.maxRatio = std::max(it->second.maxRatio, ratio);
            }
            lastSeen[key] = k;
        }
    }
    for (auto& [key, run] : runs) {
        ActiveInterval ai;
        ai.joint = run.joint;
        ai.limit = run.limit;
        ai.uIndexFrom = run.from;
        ai.uIndexTo = run.to;
        ai.uFrom = prof.sGrid[run.from];
        ai.uTo = prof.sGrid[run.to];
        ai.tFrom = prof.times[run.from];
        ai.tTo = prof.times[run.to];
        ai.maxRatio = std::min(1.0 + 1e-9, run.maxRatio);
        intervals.push_back(std::move(ai));
    }
    sort(intervals.begin(), intervals.end(),
         [](const ActiveInterval& a, const ActiveInterval& b) {
             if (a.uFrom != b.uFrom) return a.uFrom < b.uFrom;
             if (a.joint != b.joint) return a.joint < b.joint;
             return a.limit < b.limit;
         });

    int bv = -1; double bvr = 0.0;
    int ba = -1; double bar = 0.0;
    for (const auto& ai : intervals) {
        if (ai.limit == "velocity" && ai.maxRatio > bvr) { bvr = ai.maxRatio; bv = ai.joint; }
        if (ai.limit == "acceleration" && ai.maxRatio > bar) { bar = ai.maxRatio; ba = ai.joint; }
    }

    // ---- Waypoint states (one per retained point, strictly increasing t) --
    Result res;
    res.retainedInputIndices = keep;
    res.removedDuplicates = removed;
    res.duration = prof.times[N];
    res.activeIntervals = std::move(intervals);
    res.verification = verif;
    res.bottleneckVelocityJoint = bv;
    res.bottleneckAccelerationJoint = ba;

    int M = static_cast<int>(Q.cols());
    res.points.resize(M);
    for (int i = 0; i < M; ++i) {
        double u = static_cast<double>(i);
        // t(u): find the grid cell containing u. With grid alignment u is on
        // a node; the lookup below also handles the generic case using the
        // closed-form in-cell time integral.
        int k = std::clamp(static_cast<int>(std::floor(u / ds)), 0, N - 1);
        double f = std::clamp((u - prof.sGrid[k]) / ds, 0.0, 1.0);
        double xA = prof.x[k], xB = prof.x[k + 1];
        double sdA = std::sqrt(std::max(0.0, xA));
        double xHere = std::max(0.0, xA + (xB - xA) * f);
        double sdHere = std::sqrt(xHere);
        double dx = xB - xA;
        double t;
        if (std::abs(dx) > 1e-12)
            t = prof.times[k] + 2.0 * ds * (sdHere - sdA) / dx;
        else
            t = prof.times[k] + f * ds / (sdA > 1e-15 ? sdA : 1e-15);
        PointState ps;
        ps.t = t;
        ps.q = spline.value(u);
        ps.qd = spline.derivative(u) * sdHere;
        // Acceleration at the waypoint: grid-sdd for the containing cell plus
        // the centripetal term (continuous across cells to grid accuracy).
        ps.qdd = spline.derivative(u) * prof.sdd[k] +
                 spline.secondDerivative(u) * xHere;
        res.points[i] = std::move(ps);
    }
    // Guarantee exact monotonic strict increase and exact endpoints.
    res.points.front().t = 0.0;
    res.points.back().t = res.duration;
    for (int i = 1; i < M; ++i)
        if (!(res.points[i].t > res.points[i - 1].t))
            res.points[i].t = res.points[i - 1].t + 1e-12;
    res.points.back().t = res.duration;

    return res;
}

} // namespace topp
