#include "gridfusion/protocol.hpp"

#include <algorithm>
#include <cmath>
#include <sstream>

#include <nlohmann/json.hpp>

namespace gridfusion::proto {

using json = nlohmann::json;

namespace {

// Pull a named field from either a flat object or a {"geometry": {...}} /
// {"sensor": {...}} nested envelope; first hit wins.
const json* findField(const json& j, const char* key) {
    if (j.is_object()) {
        if (auto it = j.find(key); it != j.end()) return &*it;
        for (const char* env : {"geometry", "sensor"}) {
            if (auto it = j.find(env);
                it != j.end() && it->is_object()) {
                if (auto k = it->find(key); k != it->end()) return &*k;
            }
        }
    }
    return nullptr;
}

bool getDouble(const json& j, const char* key, double& out) {
    const json* v = findField(j, key);
    if (!v) return true;  // optional
    if (!v->is_number()) return false;
    out = v->get<double>();
    return true;
}

bool getInt(const json& j, const char* key, int& out) {
    const json* v = findField(j, key);
    if (!v) return true;
    if (v->is_number_integer()) {
        out = v->get<int>();
        return true;
    }
    if (v->is_number_float()) {
        const double d = v->get<double>();
        if (d == std::floor(d) && std::fabs(d) < 2.1e9) {
            out = static_cast<int>(d);
            return true;
        }
    }
    return false;
}

}  // namespace

MapConfig parseConfig(const std::string& body, ParseResult& r) {
    MapConfig cfg;
    json j;
    try {
        j = json::parse(body);
    } catch (const std::exception& e) {
        r = {false, 400, std::string("invalid JSON: ") + e.what()};
        return cfg;
    }
    if (!j.is_object()) {
        r = {false, 400, "request body must be a JSON object"};
        return cfg;
    }
    const char* required[] = {"resolution", "width", "height"};
    for (const char* k : required) {
        if (!findField(j, k)) {
            r = {false, 400, std::string("missing required field: ") + k};
            return cfg;
        }
    }
    int w = 0, h = 0;
    double res = 0.0;
    if (!getInt(j, "width", w) || !getInt(j, "height", h) ||
        !getDouble(j, "resolution", res) ||
        !getDouble(j, "origin_x", cfg.origin_x) ||
        !getDouble(j, "origin_y", cfg.origin_y) ||
        !getDouble(j, "l_occ", cfg.l_occ) ||
        !getDouble(j, "l_free", cfg.l_free) ||
        !getDouble(j, "l_min", cfg.l_min) ||
        !getDouble(j, "l_max", cfg.l_max) ||
        !getDouble(j, "default_max_range", cfg.default_max_range)) {
        r = {false, 400, "a field has the wrong type"};
        return cfg;
    }
    cfg.width = w;
    cfg.height = h;
    cfg.resolution = res;
    if (const std::string err = cfg.validate(); !err.empty()) {
        r = {false, 400, err};
        return cfg;
    }
    r.ok = true;
    return cfg;
}

Scan parseScan(const std::string& body, ParseResult& r) {
    Scan scan;
    json j;
    try {
        j = json::parse(body);
    } catch (const std::exception& e) {
        r = {false, 400, std::string("invalid JSON: ") + e.what()};
        return scan;
    }
    if (!j.is_object()) {
        r = {false, 400, "request body must be a JSON object"};
        return scan;
    }
    const json* pose = findField(j, "pose");
    if (!pose && j.find("pose") != j.end()) pose = &j.at("pose");
    if (!pose || !pose->is_object()) {
        r = {false, 400, "missing required object: pose"};
        return scan;
    }
    if (!getDouble(*pose, "x", scan.pose.x) ||
        !getDouble(*pose, "y", scan.pose.y) ||
        !getDouble(*pose, "theta", scan.pose.theta)) {
        r = {false, 400, "pose fields must be numbers"};
        return scan;
    }
    if (j.contains("max_range")) {
        if (!j["max_range"].is_number()) {
            r = {false, 400, "max_range must be a number"};
            return scan;
        }
        scan.max_range = j["max_range"].get<double>();
        if (scan.max_range <= 0.0) scan.max_range = -1.0;
    }
    const auto it = j.find("beams");
    if (it == j.end() || !it->is_array()) {
        r = {false, 400, "missing required array: beams"};
        return scan;
    }
    scan.beams.reserve(it->size());
    for (std::size_t i = 0; i < it->size(); ++i) {
        const json& b = (*it)[i];
        if (!b.is_object() || !b.contains("angle") || !b.contains("range")) {
            r = {false, 400,
                 "each beam must be an object with angle and range"};
            return scan;
        }
        if (!b["angle"].is_number() || !b["range"].is_number()) {
            r = {false, 400, "beam angle/range must be numbers"};
            return scan;
        }
        Beam beam;
        beam.angle = b["angle"].get<double>();
        beam.range = b["range"].get<double>();
        if (b.contains("no_return") && b["no_return"].is_boolean())
            beam.no_return = b["no_return"].get<bool>();
        scan.beams.push_back(beam);
    }
    r.ok = true;
    return scan;
}

std::string validateScanForMap(const Scan& scan, const MapConfig& cfg) {
    if (!std::isfinite(scan.pose.x) || !std::isfinite(scan.pose.y) ||
        !std::isfinite(scan.pose.theta))
        return "pose must contain finite numbers";
    const double mr =
        scan.max_range > 0.0 ? scan.max_range : cfg.default_max_range;
    const int cx = static_cast<int>(std::floor(
        (scan.pose.x - cfg.origin_x) / cfg.resolution));
    const int cy = static_cast<int>(std::floor(
        (scan.pose.y - cfg.origin_y) / cfg.resolution));
    if (cx < 0 || cx >= cfg.width || cy < 0 || cy >= cfg.height)
        return "sensor pose is outside the map";
    for (std::size_t i = 0; i < scan.beams.size(); ++i) {
        const Beam& b = scan.beams[i];
        if (!std::isfinite(b.angle) || !std::isfinite(b.range))
            return "beam angle/range must be finite";
        if (b.range < 0.0)
            return "beam range must be >= 0 (beam " + std::to_string(i) + ")";
        if (b.range > mr + 1e-9)
            return "beam range exceeds max_range (beam " +
                   std::to_string(i) + ")";
    }
    return "";
}

std::string configToJson(const MapConfig& cfg, const std::string& version) {
    json j;
    j["version"] = version;
    j["resolution"] = cfg.resolution;
    j["origin_x"] = cfg.origin_x;
    j["origin_y"] = cfg.origin_y;
    j["width"] = cfg.width;
    j["height"] = cfg.height;
    j["l_occ"] = cfg.l_occ;
    j["l_free"] = cfg.l_free;
    j["l_min"] = cfg.l_min;
    j["l_max"] = cfg.l_max;
    j["default_max_range"] = cfg.default_max_range;
    return j.dump(2);
}

std::string statsToJson(const ScanStats& s, const std::string& version) {
    json j;
    j["version"] = version;
    j["beams"] = s.beams;
    j["hits"] = s.hits;
    j["no_returns"] = s.no_returns;
    j["beams_clipped"] = s.beams_clipped;
    j["occupied_updates"] = s.occupied_updates;
    j["free_updates"] = s.free_updates;
    return j.dump(2);
}

namespace {

struct Bounds {
    int x0, y0, x1, y1;  // inclusive crop, in cell coordinates
    bool empty = true;
};

Bounds observedBounds(const GridMap& map) {
    Bounds b{map.width(), map.height(), -1, -1, true};
    const auto& cells = map.cells();
    for (int y = 0; y < map.height(); ++y)
        for (int x = 0; x < map.width(); ++x)
            if (cells[static_cast<std::size_t>(y) * map.width() + x]
                    .observed) {
                b.x0 = std::min(b.x0, x);
                b.y0 = std::min(b.y0, y);
                b.x1 = std::max(b.x1, x);
                b.y1 = std::max(b.y1, y);
                b.empty = false;
            }
    return b;
}

}  // namespace

std::string gridToJson(const GridMap& map, const ExportOptions& opt) {
    Bounds b;
    b.x0 = 0;
    b.y0 = 0;
    b.x1 = map.width() - 1;
    b.y1 = map.height() - 1;
    b.empty = false;
    if (opt.bbox_only) b = observedBounds(map);

    const auto probs = map.probabilities();
    const auto logits = map.logOdds();
    const auto& cells = map.cells();

    json rows = json::array();
    if (!b.empty) {
        for (int y = b.y0; y <= b.y1; ++y) {
            json row = json::array();
            for (int x = b.x0; x <= b.x1; ++x) {
                const std::size_t i =
                    static_cast<std::size_t>(y) * map.width() + x;
                if (!cells[i].observed)
                    row.push_back(nullptr);  // unknown: distinct from 0.5
                else if (opt.logodds)
                    row.push_back(logits[i]);
                else
                    row.push_back(probs[i]);
            }
            rows.push_back(std::move(row));
        }
    }
    json out;
    out["version"] = map.version_id();
    out["resolution"] = map.config().resolution;
    out["origin_x"] =
        map.config().origin_x +
        (b.empty ? 0.0 : b.x0 * map.config().resolution);
    out["origin_y"] =
        map.config().origin_y +
        (b.empty ? 0.0 : b.y0 * map.config().resolution);
    out["width"] = b.empty ? 0 : b.x1 - b.x0 + 1;
    out["height"] = b.empty ? 0 : b.y1 - b.y0 + 1;
    out["value"] = opt.logodds ? "logodds" : "probability";
    out["unknown"] = nullptr;
    out["rows"] = std::move(rows);
    return out.dump();
}

std::string gridToCsv(const GridMap& map, const ExportOptions& opt) {
    Bounds b;
    b.x0 = 0;
    b.y0 = 0;
    b.x1 = map.width() - 1;
    b.y1 = map.height() - 1;
    b.empty = false;
    if (opt.bbox_only) b = observedBounds(map);

    const auto probs = map.probabilities();
    const auto logits = map.logOdds();
    const auto& cells = map.cells();

    std::ostringstream os;
    os << "# version=" << map.version_id()
       << " resolution=" << map.config().resolution
       << " origin_x=" << map.config().origin_x
       << " origin_y=" << map.config().origin_y
       << " width=" << map.width() << " height=" << map.height()
       << " value=" << (opt.logodds ? "logodds" : "probability")
       << " unknown=?\n";
    if (!b.empty) {
        os.precision(9);
        for (int y = b.y0; y <= b.y1; ++y) {
            for (int x = b.x0; x <= b.x1; ++x) {
                const std::size_t i =
                    static_cast<std::size_t>(y) * map.width() + x;
                if (x > b.x0) os << ',';
                if (!cells[i].observed)
                    os << '?';
                else if (opt.logodds)
                    os << logits[i];
                else
                    os << probs[i];
            }
            os << '\n';
        }
    }
    return os.str();
}

std::string mapListToJson(
    const std::vector<std::pair<std::string, MapConfig>>& maps) {
    json arr = json::array();
    for (const auto& [id, cfg] : maps) {
        json j;
        j["version"] = id;
        j["resolution"] = cfg.resolution;
        j["width"] = cfg.width;
        j["height"] = cfg.height;
        arr.push_back(std::move(j));
    }
    return json{{"maps", std::move(arr)}}.dump(2);
}

}  // namespace gridfusion::proto
