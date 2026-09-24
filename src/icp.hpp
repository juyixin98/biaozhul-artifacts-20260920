// Offline rigid point-cloud registration via point-to-point ICP.
//
// Pure computation, no I/O. The estimated transform maps the *source* cloud
// onto the *target* cloud:  p_target ~ R * p_source + t.
#pragma once

#include <array>
#include <limits>
#include <string>
#include <vector>

#include <Eigen/Dense>

namespace pcrs {

struct IcpOptions {
    int max_iterations = 50;
    // <= 0 means "derive from the target cloud's median nearest-neighbour
    // spacing" so outliers and disjoint clouds are rejected by default.
    double max_correspondence_distance = 0.0;
    double transformation_epsilon = 1e-8;   // stop on small incremental pose
    double fitness_epsilon = 1e-9;          // stop on small relative RMSE change
    int min_correspondences = 3;
    // A matched set whose second-largest spread eigenvalue is below this
    // fraction of the largest is treated as collinear (rank-deficient).
    double collinear_eigenvalue_ratio = 1e-6;

    bool has_initial_guess = false;
    Eigen::Matrix3d initial_R = Eigen::Matrix3d::Identity();
    Eigen::Vector3d initial_t = Eigen::Vector3d::Zero();
};

struct IcpResult {
    Eigen::Matrix3d R = Eigen::Matrix3d::Identity();
    Eigen::Vector3d t = Eigen::Vector3d::Zero();

    double rmse = std::numeric_limits<double>::quiet_NaN();
    double inlier_ratio = 0.0;   // accepted pairs / valid source points
    int num_correspondences = 0;
    int iterations = 0;
    bool converged = false;
    bool degenerate = false;     // geometry cannot support a confident 6-DOF pose
    bool high_confidence = false;
    std::string reason;          // machine-readable termination reason

    // Diagnostics / guard rails
    double rotation_orthogonality_error = 0.0;  // ||R^T R - I||_F
    double rotation_determinant = 1.0;
    double max_correspondence_distance_used = 0.0;
    std::array<double, 3> matched_spread_eigenvalues = {0, 0, 0};
    std::array<double, 3> cross_covariance_singular_values = {0, 0, 0};

    int source_points_total = 0, source_points_valid = 0;
    int target_points_total = 0, target_points_valid = 0;
};

// Keeps only points whose three coordinates are all finite.
std::vector<Eigen::Vector3d> filterFinite(
    const std::vector<Eigen::Vector3d>& points, int* dropped = nullptr);

IcpResult runIcp(const std::vector<Eigen::Vector3d>& sourceRaw,
                 const std::vector<Eigen::Vector3d>& targetRaw,
                 const IcpOptions& opts);

// Human-readable explanation of a termination reason.
const char* reasonDescription(const std::string& reason);

}  // namespace pcrs
