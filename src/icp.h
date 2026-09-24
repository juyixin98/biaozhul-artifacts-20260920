// icp.h — offline rigid point-cloud registration with point-to-point ICP.
//
// The implementation only uses the two point clouds supplied by the caller and
// an optional initial pose. It never consults ground-truth transforms.
#pragma once
#include <Eigen/Dense>
#include <Eigen/SVD>
#include <string>
#include <vector>

namespace pcr {

constexpr size_t kMaxPoints = 5000;

struct PointCloud3 {
    // One finite 3D point per column (3 x N). Non-finite points are removed by cleanCloud().
    Eigen::Matrix3Xd points{3, 0};
    size_t droppedNonFinite = 0;  // number of input rows discarded
};

struct IcpOptions {
    size_t maxIterations = 50;            // hard-capped at 200
    double maxCorrespondenceDistance = -1.0;  // <0 => adaptive from cloud scale
    double relativeFitnessTolerance = 1e-6;
    double transformTolerance = 1e-6;     // rotation(rad) and translation normalized by scale
    double rankTolerance = 1e-3;          // sigma_min / sigma_max below this => rank-deficient
    bool verbose = false;
};

// Machine-readable convergence / termination reason.
enum class Reason {
    Converged,                  // relative RMSE + transform increments below tolerances
    MaxIterations,              // iteration budget exhausted
    InsufficientCorrespondences,
    DegenerateGeometry,         // collinear/planar/repeated near-zero singular values
    NoOverlap,                  // zero pairs within the correspondence gate
    InvalidInput
};

const char* reasonName(Reason r);

struct IcpResult {
    bool ok = false;
    Reason reason = Reason::InvalidInput;
    std::string error;          // populated when !ok

    Eigen::Matrix3d rotation = Eigen::Matrix3d::Identity();
    Eigen::Vector3d translation = Eigen::Vector3d::Zero();

    size_t iterations = 0;
    double rmse = 0.0;          // RMS residual over the final inlier pairs
    double inlierRatio = 0.0;   // final pairs / source point count (after cleaning)
    size_t correspondences = 0;
    bool converged = false;
    bool degenerate = false;    // geometry could not constrain all 6 DOF
    bool highConfidence = false;
    double rankCondition = 0.0; // sigma_min / sigma_max of the final H matrix
    double scale = 1.0;         // characteristic scale used for normalization
};

// Loads rows of exactly 3 finite numbers into a PointCloud3; counts dropped rows.
PointCloud3 cleanCloud(const std::vector<Eigen::Vector3d>& raw);

// Registers source onto target: transformed_source = R * source + t.
// initialPose (4x4) is optional; identity is used when nullptr.
IcpResult runIcp(const PointCloud3& source,
                 const PointCloud3& target,
                 const IcpOptions& opts = {},
                 const Eigen::Matrix4d* initialPose = nullptr);

}  // namespace pcr
