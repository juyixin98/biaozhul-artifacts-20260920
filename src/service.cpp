#include "service.hpp"

#include <algorithm>
#include <cmath>
#include <stdexcept>

namespace pf {

Beam beamFromReturn(const Pose& pose, const SensorReturn& ret,
                    double max_range) {
    Beam b;
    b.ox = pose.x;
    b.oy = pose.y;
    double a = pose.theta + ret.angle;
    bool hit = ret.range >= 0.0 && ret.range < max_range;
    if (hit) {
        b.hit = true;
        b.ex = pose.x + ret.range * std::cos(a);
        b.ey = pose.y + ret.range * std::sin(a);
        b.max_range = max_range;
    } else {
        // Free-only virtual ray along the beam direction.
        b.hit = false;
        b.ex = pose.x + max_range * std::cos(a);
        b.ey = pose.y + max_range * std::sin(a);
        b.max_range = max_range;
    }
    return b;
}

MapRecord& MapService::createMap(const CreateMapOptions& opt,
                                 std::int64_t now_unix) {
    if (opt.width <= 0 || opt.height <= 0)
        throw std::invalid_argument("width/height must be positive");
    if (!(opt.resolution > 0.0))
        throw std::invalid_argument("resolution must be positive");
    if (!(opt.p_hit > 0.5 && opt.p_hit < 1.0))
        throw std::invalid_argument("p_hit must be in (0.5, 1)");
    if (!(opt.p_free > 0.0 && opt.p_free < 0.5))
        throw std::invalid_argument("p_free must be in (0, 0.5)");
    if (!(opt.l_max > 0.0))
        throw std::invalid_argument("l_max must be positive");

    MapConfig cfg;
    cfg.width = opt.width;
    cfg.height = opt.height;
    cfg.resolution = opt.resolution;
    cfg.origin_x = opt.origin_x;
    cfg.origin_y = opt.origin_y;

    FusionParams params;
    params.l_hit = OccupancyGrid::logit(opt.p_hit);
    params.l_free = OccupancyGrid::logit(opt.p_free);
    params.l_max = opt.l_max;

    auto rec = std::make_unique<MapRecord>();
    rec->id = crypto::b64url_encode(crypto::secure_random_bytes(12));
    rec->config = cfg;
    rec->params = params;
    rec->created_unix = now_unix;
    rec->grid = std::make_unique<OccupancyGrid>(cfg, params);
    initializeEmptyVersion(*rec);

    std::lock_guard<std::mutex> lock(mu_);
    std::string id = rec->id;
    auto [it, inserted] = maps_.emplace(id, std::move(rec));
    return *it->second;
}

MapRecord* MapService::find(const std::string& id) {
    std::lock_guard<std::mutex> lock(mu_);
    auto it = maps_.find(id);
    return it == maps_.end() ? nullptr : it->second.get();
}

std::vector<std::string> MapService::listIds() const {
    std::lock_guard<std::mutex> lock(mu_);
    std::vector<std::string> ids;
    ids.reserve(maps_.size());
    for (const auto& [id, ptr] : maps_) ids.push_back(id);
    return ids;
}

}  // namespace pf
