#include "icp.hpp"

#include <algorithm>
#include <cmath>

#include "kdtree.hpp"

namespace pcrs {

std::vector<Eigen::Vector3d> filterFinite(
    const std::vector<Eigen::Vector3d>& points, int* dropped) {
    std::vector<Eigen::Vector3d> out;
    out.reserve(points.size());
    int bad = 0;
    for (const auto& p : points) {
        if (std::isfinite(p.x()) && std::isfinite(p.y()) &&
            std::isfinite(p.z())) {
            out.push_back(p);
        } else {
            ++bad;
        }
    }
    if (dropped) *dropped = bad;
    return out;
}

const char* reasonDescription(const std::string& reason) {
    if (reason == "insufficient_points")
        return "Fewer than 3 finite points in one of the clouds.";
    if (reason == "no_correspondences")
        return "No source point found a target neighbour within "
               "max_correspondence_distance on the first iteration.";
    if (reason == "correspondences_lost")
        return "The number of in-range correspondences dropped below the "
               "minimum after a pose update (clouds likely do not overlap).";
    if (reason == "converged_transformation_epsilon")
        return "Incremental rotation and translation both fell below "
               "transformation_epsilon.";
    if (reason == "converged_fitness_epsilon")
        return "Relative RMSE change fell below fitness_epsilon.";
    if (reason == "max_iterations")
        return "Stopped after max_iterations without meeting a convergence "
               "criterion.";
    return "Unknown reason.";
}

namespace {

// Median nearest-neighbour spacing of the target cloud, O(n^2); n <= 5000.
double medianNearestSpacing(const std::vector<Eigen::Vector3d>& pts) {
    const size_t n = pts.size();
    std::vector<double> mins(n, std::numeric_limits<double>::infinity());
    for (size_t i = 0; i < n; ++i) {
        for (size_t j = i + 1; j < n; ++j) {
            const double d2 = (pts[i] - pts[j]).squaredNorm();
            if (d2 < mins[i]) mins[i] = d2;
            if (d2 < mins[j]) mins[j] = d2;
        }
    }
    for (double& v : mins) v = std::sqrt(v);
    std::sort(mins.begin(), mins.end());
    return mins.empty() ? 0.0 : mins[mins.size() / 2];
}

}  // namespace

IcpResult runIcp(const std::vector<Eigen::Vector3d>& sourceRaw,
                 const std::vector<Eigen::Vector3d>& targetRaw,
                 const IcpOptions& opts) {
    IcpResult res;
    res.source_points_total = static_cast<int>(sourceRaw.size());
    res.target_points_total = static_cast<int>(targetRaw.size());

    int srcDropped = 0, tgtDropped = 0;
    auto source = filterFinite(sourceRaw, &srcDropped);
    auto target = filterFinite(targetRaw, &tgtDropped);
    res.source_points_valid = static_cast<int>(source.size());
    res.target_points_valid = static_cast<int>(target.size());

    res.R = opts.has_initial_guess ? opts.initial_R
                                   : Eigen::Matrix3d::Identity();
    res.t = opts.has_initial_guess ? opts.initial_t : Eigen::Vector3d::Zero();

    if (static_cast<int>(source.size()) < opts.min_correspondences ||
        static_cast<int>(target.size()) < opts.min_correspondences) {
        res.reason = "insufficient_points";
        res.rotation_orthogonality_error =
            (res.R.transpose() * res.R - Eigen::Matrix3d::Identity()).norm();
        res.rotation_determinant = res.R.determinant();
        return res;
    }

    // --- Correspondence gate: explicit user value or data-derived scale. ---
    double maxDist = opts.max_correspondence_distance;
    double sceneExtent = 1.0;
    if (!(maxDist > 0.0)) {
        const double medianNN = medianNearestSpacing(target);
        if (medianNN > 0.0) {
            maxDist = 5.0 * medianNN;
        } else {
            // Duplicate/degenerate spacing: fall back to bbox diagonal.
            Eigen::Vector3d mn = target[0], mx = target[0];
            for (const auto& p : target) {
                mn = mn.cwiseMin(p);
                mx = mx.cwiseMax(p);
            }
            sceneExtent = (mx - mn).norm();
            maxDist = std::max(1e-9, 0.05 * sceneExtent);
        }
    }
    res.max_correspondence_distance_used = maxDist;
    const double maxDist2 = maxDist * maxDist;

    const KdTree tree(target);

    Eigen::Matrix3d Rcur = res.R;
    Eigen::Vector3d tcur = res.t;

    double prevRmse = std::numeric_limits<double>::infinity();

    std::vector<Eigen::Vector3d> matchedSrc, matchedTgt;

    for (int iter = 0; iter < opts.max_iterations; ++iter) {
        matchedSrc.clear();
        matchedTgt.clear();
        matchedSrc.reserve(source.size());
        matchedTgt.reserve(source.size());

        for (const auto& ps : source) {
            const Eigen::Vector3d q = Rcur * ps + tcur;
            const auto nn = tree.nearest(q);
            if (nn.second <= maxDist2) {
                matchedSrc.push_back(ps);
                matchedTgt.push_back(target[nn.first]);
            }
        }

        const int m = static_cast<int>(matchedSrc.size());
        if (m < opts.min_correspondences) {
            res.iterations = iter;
            res.reason = (iter == 0) ? "no_correspondences"
                                    : "correspondences_lost";
            res.num_correspondences = m;
            res.inlier_ratio = static_cast<double>(m) / source.size();
            res.rotation_orthogonality_error =
                (Rcur.transpose() * Rcur - Eigen::Matrix3d::Identity()).norm();
            res.rotation_determinant = Rcur.determinant();
            res.R = Rcur;
            res.t = tcur;
            return res;
        }

        // Centroids of the matched sets.
        Eigen::Vector3d muS = Eigen::Vector3d::Zero();
        Eigen::Vector3d muT = Eigen::Vector3d::Zero();
        for (int i = 0; i < m; ++i) {
            muS += matchedSrc[i];
            muT += matchedTgt[i];
        }
        muS /= m;
        muT /= m;

        // Cross-covariance H = sum (tgt-muT)(src-muS)^T.
        Eigen::Matrix3d H = Eigen::Matrix3d::Zero();
        Eigen::Matrix3d spread = Eigen::Matrix3d::Zero();
        for (int i = 0; i < m; ++i) {
            const Eigen::Vector3d ds = matchedSrc[i] - muS;
            H += (matchedTgt[i] - muT) * ds.transpose();
            spread += ds * ds.transpose();
        }

        Eigen::JacobiSVD<Eigen::Matrix3d> svd(
            H, Eigen::ComputeFullU | Eigen::ComputeFullV);
        const Eigen::Vector3d sv = svd.singularValues();
        Eigen::Matrix3d U = svd.matrixU();
        Eigen::Matrix3d V = svd.matrixV();
        Eigen::Matrix3d Rinc = U * V.transpose();
        if (Rinc.determinant() < 0.0) {
            // Reflection fix: flip the singular vector of the smallest value.
            U.col(2) *= -1.0;
            Rinc = U * V.transpose();
        }
        const Eigen::Vector3d tinc = muT - Rinc * muS;

        // Post-update residuals on the established correspondences.
        double se = 0.0;
        for (int i = 0; i < m; ++i) {
            const Eigen::Vector3d d =
                matchedTgt[i] - (Rinc * matchedSrc[i] + tinc);
            se += d.squaredNorm();
        }
        const double rmse = std::sqrt(se / m);

        // Pose update.
        tcur = Rinc * tcur + tinc;
        Rcur = Rinc * Rcur;

        res.iterations = iter + 1;
        res.num_correspondences = m;
        res.inlier_ratio = static_cast<double>(m) / source.size();
        res.rmse = rmse;
        res.cross_covariance_singular_values = {sv(0), sv(1), sv(2)};

        // Rank/geometry diagnostics of the matched source spread.
        Eigen::SelfAdjointEigenSolver<Eigen::Matrix3d> es(spread);
        std::array<double, 3> ev = {es.eigenvalues()(2),
                                    es.eigenvalues()(1),
                                    es.eigenvalues()(0)};  // descending
        res.matched_spread_eigenvalues = ev;

        res.R = Rcur;
        res.t = tcur;
        res.rotation_orthogonality_error =
            (Rcur.transpose() * Rcur - Eigen::Matrix3d::Identity()).norm();
        res.rotation_determinant = Rcur.determinant();

        // Collinear / rank-deficient matched set: rotation is not observable.
        const double rankFloor =
            opts.collinear_eigenvalue_ratio * std::max(ev[0], 1e-300);
        res.degenerate = (ev[1] <= rankFloor) || (sv(1) <= 1e-12 * std::max(sv(0), 1e-300));

        // Convergence: incremental motion.
        const double cosA =
            std::max(-1.0, std::min(1.0, (Rinc.trace() - 1.0) / 2.0));
        const double angle = std::acos(cosA);
        const double tEps =
            opts.transformation_epsilon * std::max(1.0, sceneExtent);
        if (angle <= opts.transformation_epsilon && tinc.norm() <= tEps) {
            res.converged = true;
            res.reason = "converged_transformation_epsilon";
            break;
        }
        if (std::isfinite(prevRmse) && prevRmse > 0.0 &&
            std::fabs(prevRmse - rmse) / prevRmse < opts.fitness_epsilon) {
            res.converged = true;
            res.reason = "converged_fitness_epsilon";
            break;
        }
        prevRmse = rmse;

        if (iter + 1 == opts.max_iterations) {
            res.reason = "max_iterations";
        }
    }

    res.high_confidence =
        res.converged && !res.degenerate &&
        res.inlier_ratio >= 0.5 &&
        res.num_correspondences >= opts.min_correspondences &&
        std::isfinite(res.rmse);

    return res;
}

}  // namespace pcrs
