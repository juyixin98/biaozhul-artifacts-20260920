#include "icp.h"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <limits>
#include <unordered_map>

namespace pcr {

const char* reasonName(Reason r) {
    switch (r) {
        case Reason::Converged: return "converged";
        case Reason::MaxIterations: return "max_iterations";
        case Reason::InsufficientCorrespondences: return "insufficient_correspondences";
        case Reason::DegenerateGeometry: return "degenerate_geometry";
        case Reason::NoOverlap: return "no_overlap";
        case Reason::InvalidInput: return "invalid_input";
    }
    return "invalid_input";
}

namespace {

bool finiteVec(const Eigen::Vector3d& v) {
    return std::isfinite(v.x()) && std::isfinite(v.y()) && std::isfinite(v.z());
}

// Nearest rotation to an arbitrary 3x3 matrix M in Frobenius norm: if
// SVD(M) = U S V^T then R = U diag(1,1,d) V^T, d = sign(det(U V^T)).
Eigen::Matrix3d projectRotation(const Eigen::Matrix3d& M) {
    Eigen::JacobiSVD<Eigen::Matrix3d> svd(M, Eigen::ComputeFullU | Eigen::ComputeFullV);
    Eigen::Matrix3d d = Eigen::Matrix3d::Identity();
    d(2, 2) = (svd.matrixU() * svd.matrixV().transpose()).determinant() < 0.0 ? -1.0 : 1.0;
    return svd.matrixU() * d * svd.matrixV().transpose();
}

double cloudScale(const Eigen::Matrix3Xd& p) {
    if (p.cols() == 0) return 0.0;
    Eigen::Vector3d mn = p.rowwise().minCoeff();
    Eigen::Vector3d mx = p.rowwise().maxCoeff();
    return (mx - mn).norm();
}

// Uniform-voxel hash table giving nearest-neighbor queries within a radius.
class VoxelGrid {
public:
    VoxelGrid(const Eigen::Matrix3Xd& pts, double cell) : pts_(pts), cell_(cell), inv_(1.0 / cell) {
        cells_.reserve(static_cast<size_t>(pts.cols() * 2));
        for (int i = 0; i < pts.cols(); ++i) {
            cells_[keyOf(pts_.col(i))].push_back(i);
        }
    }

    // Returns nearest index with distance < bestDist; false if none within that radius.
    bool nearestWithin(const Eigen::Vector3d& q, double bestDist, int& bestIdx) const {
        Eigen::Vector3i c(static_cast<int>(std::floor(q.x() * inv_)),
                          static_cast<int>(std::floor(q.y() * inv_)),
                          static_cast<int>(std::floor(q.z() * inv_)));
        double bestSq = bestDist * bestDist;
        bool found = false;
        for (int dz = -1; dz <= 1; ++dz)
            for (int dy = -1; dy <= 1; ++dy)
                for (int dx = -1; dx <= 1; ++dx) {
                    auto it = cells_.find((c + Eigen::Vector3i(dx, dy, dz)).eval());
                    if (it == cells_.end()) continue;
                    for (int idx : it->second) {
                        double d2 = (pts_.col(idx) - q).squaredNorm();
                        if (d2 < bestSq) {
                            bestSq = d2;
                            found = true;
                            bestIdx = idx;
                        }
                    }
                }
        return found;
    }

private:
    struct KeyHash {
        size_t operator()(const Eigen::Vector3i& v) const noexcept {
            auto mix = [](int64_t x) {
                x += 0x9e3779b97f4a7c15ULL;
                x = (x ^ (x >> 30)) * 0xbf58476d1ce4e5b9ULL;
                x = (x ^ (x >> 27)) * 0x94d049bb133111ebULL;
                return static_cast<size_t>(x ^ (x >> 31));
            };
            return mix(static_cast<int64_t>(v.x())) ^
                   (mix(static_cast<int64_t>(v.y()) + 0x123456789abcdef0ULL) << 1) ^
                   (mix(static_cast<int64_t>(v.z()) + 0x0fedcba987654321ULL) >> 1);
        }
    };

    using Key = Eigen::Vector3i;
    Eigen::Vector3i keyOf(const Eigen::Vector3d& p) const {
        return Eigen::Vector3i(static_cast<int>(std::floor(p.x() * inv_)),
                               static_cast<int>(std::floor(p.y() * inv_)),
                               static_cast<int>(std::floor(p.z() * inv_)));
    }

    const Eigen::Matrix3Xd& pts_;
    double cell_;
    double inv_;
    std::unordered_map<Key, std::vector<int>, KeyHash> cells_;
};

struct PairSet {
    std::vector<int> src;
    std::vector<int> tgt;
};

PairSet findPairs(const Eigen::Matrix3Xd& moved, const VoxelGrid& grid, double maxDist) {
    PairSet ps;
    ps.src.reserve(static_cast<size_t>(moved.cols()));
    ps.tgt.reserve(static_cast<size_t>(moved.cols()));
    for (int i = 0; i < moved.cols(); ++i) {
        int j = -1;
        if (grid.nearestWithin(moved.col(i), maxDist, j)) {
            ps.src.push_back(i);
            ps.tgt.push_back(j);
        }
    }
    return ps;
}

// Best rigid transform X(3xk) -> Y(3xk); returns singular values of H.
struct KabschResult {
    Eigen::Matrix3d R;
    Eigen::Vector3d t;
    Eigen::Vector3d sv;
};

KabschResult kabsch(const Eigen::Matrix3Xd& X, const Eigen::Matrix3Xd& Y) {
    Eigen::Vector3d mx = X.rowwise().mean();
    Eigen::Vector3d my = Y.rowwise().mean();
    Eigen::Matrix3Xd Xc = X.colwise() - mx;
    Eigen::Matrix3Xd Yc = Y.colwise() - my;
    Eigen::Matrix3d H = Xc * Yc.transpose();
    Eigen::JacobiSVD<Eigen::Matrix3d> svd(H, Eigen::ComputeFullU | Eigen::ComputeFullV);
    Eigen::Matrix3d d = Eigen::Matrix3d::Identity();
    // H = Xc Yc^T; optimal R aligning X onto Y is V diag(1,1,d) U^T.
    d(2, 2) = (svd.matrixV() * svd.matrixU().transpose()).determinant() < 0 ? -1.0 : 1.0;
    KabschResult r;
    r.R = svd.matrixV() * d * svd.matrixU().transpose();
    r.t = my - r.R * mx;
    r.sv = svd.singularValues();
    return r;
}

double pairRmse(const Eigen::Matrix3Xd& moved, const Eigen::Matrix3Xd& target, const PairSet& ps) {
    double s = 0.0;
    for (size_t k = 0; k < ps.src.size(); ++k)
        s += (moved.col(ps.src[k]) - target.col(ps.tgt[k])).squaredNorm();
    return std::sqrt(s / static_cast<double>(ps.src.size()));
}

}  // namespace

PointCloud3 cleanCloud(const std::vector<Eigen::Vector3d>& raw) {
    PointCloud3 out;
    std::vector<Eigen::Vector3d> keep;
    keep.reserve(raw.size());
    for (const auto& v : raw) {
        if (finiteVec(v))
            keep.push_back(v);
        else
            ++out.droppedNonFinite;
    }
    out.points.resize(3, static_cast<int>(keep.size()));
    for (size_t i = 0; i < keep.size(); ++i) out.points.col(static_cast<int>(i)) = keep[i];
    return out;
}

IcpResult runIcp(const PointCloud3& sourceIn, const PointCloud3& targetIn,
                 const IcpOptions& opts, const Eigen::Matrix4d* initialPose) {
    IcpResult res;

    const Eigen::Matrix3Xd& src0 = sourceIn.points;
    const Eigen::Matrix3Xd& tgt = targetIn.points;
    if (src0.cols() < 3 || tgt.cols() < 3) {
        res.error = "each cloud must contain at least 3 finite points after filtering";
        res.reason = Reason::InvalidInput;
        return res;
    }
    if (opts.maxIterations == 0) {
        res.error = "max_iterations must be > 0";
        return res;
    }
    const size_t maxIter = std::min<size_t>(opts.maxIterations, 200);

    const double scale = std::max(cloudScale(src0), cloudScale(tgt));
    res.scale = scale;
    if (!(scale > 0.0) || !std::isfinite(scale)) {
        res.error = "point cloud has zero spatial extent";
        res.reason = Reason::DegenerateGeometry;
        res.degenerate = true;
        return res;
    }

    double gate = opts.maxCorrespondenceDistance;
    if (!(gate > 0.0) || !std::isfinite(gate)) gate = 0.1 * scale;  // adaptive default
    gate = std::min(gate, 0.5 * scale);  // a gate beyond this makes "inliers" meaningless

    Eigen::Matrix3d R = Eigen::Matrix3d::Identity();
    Eigen::Vector3d t = Eigen::Vector3d::Zero();
    if (initialPose) {
        R = initialPose->topLeftCorner<3, 3>();
        t = initialPose->topRightCorner<3, 1>();
        if (!R.allFinite() || !t.allFinite()) {
            res.error = "initial pose contains non-finite values";
            return res;
        }
        R = projectRotation(R);  // never trust a caller-supplied matrix to be a rotation
    }

    VoxelGrid grid(tgt, gate);
    double prevRmse = std::numeric_limits<double>::infinity();
    int rankBadStreak = 0;
    res.reason = Reason::MaxIterations;  // default if the loop exhausts its budget

    auto moved = [&]() { return (R * src0).colwise() + t; };

    for (size_t it = 0; it < maxIter; ++it) {
        Eigen::Matrix3Xd cur = moved();
        PairSet ps = findPairs(cur, grid, gate);

        if (ps.src.empty()) {
            res.reason = Reason::NoOverlap;
            res.error = "no source point lies within the correspondence distance";
            break;
        }
        if (ps.src.size() < 3) {
            res.reason = Reason::InsufficientCorrespondences;
            res.error = "fewer than 3 correspondence pairs; pose is underconstrained";
            break;
        }

        Eigen::Matrix3Xd X(3, static_cast<int>(ps.src.size()));
        Eigen::Matrix3Xd Y(3, static_cast<int>(ps.tgt.size()));
        for (size_t k = 0; k < ps.src.size(); ++k) {
            X.col(static_cast<int>(k)) = cur.col(ps.src[k]);
            Y.col(static_cast<int>(k)) = tgt.col(ps.tgt[k]);
        }

        KabschResult kb = kabsch(X, Y);
        const double smax = kb.sv(0);
        const double smin = kb.sv(2);
        res.rankCondition = (smax > 0.0) ? smin / smax : 0.0;

        if (!(smax > 1e-12 * scale) || res.rankCondition < opts.rankTolerance) {
            ++rankBadStreak;
            if (rankBadStreak >= 3) {
                res.reason = Reason::DegenerateGeometry;
                res.degenerate = true;
                res.error = "cross-covariance is rank-deficient (collinear/planar/poorly constrained data)";
                break;
            }
            // An ill-conditioned single step (often outlier-induced) is skipped, not trusted.
            continue;
        }
        rankBadStreak = 0;

        double rmse = pairRmse(cur, tgt, ps);
        // Fitness progress is measured on an absolute, scale-normalized scale.
        // Dividing by the current RMSE diverges once RMSE approaches machine
        // precision, so a separate numerical-floor check handles exact data.
        double absDelta = std::isfinite(prevRmse) ? std::abs(prevRmse - rmse)
                                                  : std::numeric_limits<double>::infinity();
        bool fitnessStalled = absDelta <= opts.relativeFitnessTolerance * scale;
        bool numericalFloor = rmse <= 1e-11 * scale && absDelta <= 1e-10 * scale;

        Eigen::Matrix3d Rnew = kb.R * R;
        Eigen::Vector3d tnew = kb.R * t + kb.t;
        Eigen::Matrix3d dR = kb.R;
        double cosAng = (dR.trace() - 1.0) * 0.5;
        cosAng = std::max(-1.0, std::min(1.0, cosAng));
        double angDelta = std::acos(cosAng);
        double transDelta = (tnew - t).norm() / scale;

        R = Rnew;
        t = tnew;
        res.iterations = it + 1;
        prevRmse = rmse;

        if (opts.verbose) {
            std::fprintf(stderr, "[icp] it=%zu pairs=%zu rmse=%.6g dRmse=%.3g dRot=%.3g dT=%.3g svr=%.3g\n",
                         it + 1, ps.src.size(), rmse,
                         std::isfinite(absDelta) ? absDelta : -1.0,
                         angDelta, transDelta, res.rankCondition);
        }

        if ((fitnessStalled || numericalFloor) &&
            angDelta < opts.transformTolerance && transDelta < opts.transformTolerance) {
            res.reason = Reason::Converged;
            break;
        }
    }

    // Finalize: exact rotation (polar projection keeps R orthogonal with det = +1).
    R = projectRotation(R);
    res.rotation = R;
    res.translation = t;

    Eigen::Matrix3Xd cur = moved();
    PairSet finalPairs = findPairs(cur, grid, gate);
    res.correspondences = finalPairs.src.size();
    res.inlierRatio = static_cast<double>(finalPairs.src.size()) / static_cast<double>(src0.cols());
    res.rmse = finalPairs.src.empty() ? std::numeric_limits<double>::infinity()
                                      : pairRmse(cur, tgt, finalPairs);
    res.converged = (res.reason == Reason::Converged);
    res.highConfidence = res.converged && !res.degenerate &&
                         res.correspondences >= 3 &&
                         res.inlierRatio >= 0.5 &&
                         res.rmse <= 0.05 * scale;
    res.ok = (res.reason == Reason::Converged || res.reason == Reason::MaxIterations);
    return res;
}

}  // namespace pcr
