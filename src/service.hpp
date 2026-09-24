// Map store: in-memory, thread-safe collection of versioned maps.
//
// Resolution + origin (+ grid size + fusion params) are bound at creation;
// attempts to apply a scan that declares mismatching geometry are rejected
// rather than silently mixing cells into an incompatible grid.
#pragma once

#include "fusion.hpp"
#include "types.hpp"

#include <map>
#include <memory>
#include <mutex>
#include <string>
#include <vector>

namespace pf {

struct CreateMapOptions {
    int width = 0;
    int height = 0;
    double resolution = 0.1;
    double origin_x = 0.0;
    double origin_y = 0.0;
    double p_hit = 0.6;
    double p_free = 0.4;
    double l_max = 3.0;
};

class MapService {
public:
    MapService() = default;

    // Creates a map with a random id; returns a borrowed pointer (owned by the
    // service). Thread-safe.
    MapRecord& createMap(const CreateMapOptions& opt, std::int64_t now_unix);

    // nullptr if absent.
    MapRecord* find(const std::string& id);
    std::vector<std::string> listIds() const;

private:
    mutable std::mutex mu_;
    std::map<std::string, std::unique_ptr<MapRecord>> maps_;
};

// Derive world-space beams from a sensor frame:
//   range < 0 or range > max_range  => no-return (free-only) beam
// Angles are in radians relative to pose.theta, equally listed.
struct SensorReturn {
    double angle;    // relative beam angle (rad)
    double range;    // measured range; <0 or >= max_range means "no return"
};

Beam beamFromReturn(const Pose& pose, const SensorReturn& ret,
                    double max_range);

}  // namespace pf
