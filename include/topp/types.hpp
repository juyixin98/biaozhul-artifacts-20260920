#pragma once
#include <stdexcept>
#include <string>
#include <vector>
#include <Eigen/Dense>

namespace topp {

// Error codes are part of the HTTP protocol contract (see README).
enum class ErrorCode {
    BAD_REQUEST,            // malformed / missing fields / wrong shapes (HTTP 400)
    INFEASIBLE_LIMITS,      // non-positive limits, too few points, ... (HTTP 422)
    INFEASIBLE_BOUNDARY,    // requested start velocity exceeds MVC (HTTP 422)
    INFEASIBLE_PROFILE,     // acceleration limits make path untrackable (HTTP 422)
    INTERNAL_ERROR          // (HTTP 500)
};

struct ApiError : std::runtime_error {
    ErrorCode code;
    ApiError(ErrorCode c, const std::string& msg)
        : std::runtime_error(msg), code(c) {}
};

// One reported interval (in path-coordinate u and in time t) during which a
// particular joint saturates a particular limit.
struct ActiveInterval {
    int joint;
    std::string limit;      // "velocity" or "acceleration"
    int uIndexFrom;         // grid index interval start (inclusive)
    int uIndexTo;           // grid index interval end (exclusive)
    double uFrom, uTo;      // path coordinate [0, U]
    double tFrom, tTo;      // trajectory time [s]
    double maxRatio;        // utilization in [0,1] of the limit inside the interval
};

struct Verification {
    bool passed;
    double maxVelocityRatio;     // max over all joints and dense samples
    int maxVelocityJoint;
    double maxAccelerationRatio;
    int maxAccelerationJoint;
    int denseSamples;            // number of samples actually checked
    std::string detail;
};

struct PointState {
    double t;
    Eigen::VectorXd q;
    Eigen::VectorXd qd;   // joint velocities
    Eigen::VectorXd qdd;  // joint accelerations
};

struct Result {
    std::vector<PointState> points;         // one state per retained waypoint
    std::vector<int> retainedInputIndices;  // source indices in the request
    int removedDuplicates;
    double duration;
    std::vector<ActiveInterval> activeIntervals;
    Verification verification;
    // Index-limiting joint/interval summary (convenience fields).
    int bottleneckVelocityJoint = -1;
    int bottleneckAccelerationJoint = -1;
};

} // namespace topp
