// Timestamped 3D pose trajectory with linear position interpolation and
// quaternion SLERP orientation interpolation.
//
// Policies (explicit, see README):
//   - Keyframe timestamps must be strictly increasing after sorting;
//     duplicate timestamps are rejected at load time (error, never silently
//     merged).
//   - Extrapolation: "clamp" (default) holds the first/last pose outside
//     the keyframe time range; "error" rejects out-of-range queries.
//   - Data gaps: if options.max_gap_seconds is set, any keyframe interval
//     longer than that is a gap; queries strictly inside a gap interval are
//     rejected with error code "gap". Queries exactly on a keyframe always
//     succeed.
//   - A single keyframe yields a constant trajectory (any in-policy query
//     returns that pose).
#pragma once

#include <optional>
#include <string>
#include <vector>

#include "geometry.hpp"

namespace traj {

struct Pose {
    double t = 0.0;  // seconds
    Vec3 position;   // meters, right-handed world frame
    Quat orientation;  // unit quaternion (w, x, y, z); normalized at load
};

enum class ExtrapolationPolicy { Clamp, Error };

struct Options {
    ExtrapolationPolicy extrapolation = ExtrapolationPolicy::Clamp;
    std::optional<double> max_gap_seconds;  // unset = no gap checking
};

struct QueryResult {
    bool ok = false;
    std::string error_code;  // "out_of_range" | "gap"
    std::string error_message;
    Pose pose;  // valid only when ok == true
};

class Trajectory {
public:
    // Throws std::invalid_argument on empty input, duplicate timestamps,
    // non-finite values, or zero-norm quaternions.
    Trajectory(std::vector<Pose> keyframes, Options options);

    // Interpolate at time t. Never throws for finite t; policy violations
    // are reported via QueryResult::ok == false.
    QueryResult query(double t) const;

    const std::vector<Pose>& keyframes() const { return keys_; }
    const Options& options() const { return options_; }

private:
    std::vector<Pose> keys_;  // sorted by t, strictly increasing
    Options options_;
};

}  // namespace traj
