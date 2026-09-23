#include "trajectory.hpp"

#include <algorithm>
#include <cmath>
#include <stdexcept>

namespace traj {

namespace {

bool finite(double v) { return std::isfinite(v); }

bool finite(const Pose& p) {
    return finite(p.t) && finite(p.position.x) && finite(p.position.y) && finite(p.position.z) &&
           finite(p.orientation.w) && finite(p.orientation.x) && finite(p.orientation.y) &&
           finite(p.orientation.z);
}

}  // namespace

Trajectory::Trajectory(std::vector<Pose> keyframes, Options options)
    : keys_(std::move(keyframes)), options_(options) {
    if (keys_.empty()) throw std::invalid_argument("trajectory requires at least one keyframe");

    for (const Pose& p : keys_) {
        if (!finite(p)) throw std::invalid_argument("non-finite value in keyframe");
    }

    std::sort(keys_.begin(), keys_.end(),
              [](const Pose& a, const Pose& b) { return a.t < b.t; });

    for (size_t i = 1; i < keys_.size(); ++i) {
        if (keys_[i].t == keys_[i - 1].t)
            throw std::invalid_argument("duplicate timestamp " + std::to_string(keys_[i].t));
    }

    for (Pose& p : keys_) p.orientation = p.orientation.normalized();
}

QueryResult Trajectory::query(double t) const {
    QueryResult r;
    if (!finite(t)) {
        r.error_code = "invalid_query";
        r.error_message = "query time is not finite";
        return r;
    }

    const double t0 = keys_.front().t;
    const double t1 = keys_.back().t;

    // Out-of-range handling per extrapolation policy.
    if (t < t0 || t > t1) {
        if (options_.extrapolation == ExtrapolationPolicy::Error) {
            r.error_code = "out_of_range";
            r.error_message = "query time outside keyframe range";
            return r;
        }
        r.ok = true;
        r.pose = (t < t0) ? keys_.front() : keys_.back();
        r.pose.t = t;
        return r;
    }

    // Exact keyframe hit (also covers the single-keyframe case): return the
    // stored pose verbatim so endpoints are bit-exact.
    auto upper = std::upper_bound(keys_.begin(), keys_.end(), t,
                                  [](double v, const Pose& p) { return v < p.t; });
    if (upper != keys_.begin() && (upper - 1)->t == t) {
        r.ok = true;
        r.pose = *(upper - 1);
        return r;
    }

    // Interior: upper is the first keyframe with key.t > t.
    const Pose& b = *upper;
    const Pose& a = *(upper - 1);

    if (options_.max_gap_seconds && (b.t - a.t) > *options_.max_gap_seconds) {
        r.error_code = "gap";
        r.error_message = "query falls inside a data gap longer than max_gap_seconds";
        return r;
    }

    double u = (t - a.t) / (b.t - a.t);  // in (0,1)
    r.ok = true;
    r.pose.t = t;
    r.pose.position = lerp(a.position, b.position, u);
    r.pose.orientation = slerp(a.orientation, b.orientation, u);
    return r;
}

}  // namespace traj
