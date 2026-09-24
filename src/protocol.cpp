#include "protocol.hpp"

#include <algorithm>
#include <cctype>
#include <sstream>
#include <stdexcept>

namespace pf {

namespace {

double reqDouble(const Json& j, const char* key) {
    if (!j.contains(key) || !j[key].is_number())
        throw ProtocolError{400, "INVALID_REQUEST",
                            std::string("missing/invalid number '") + key + "'"};
    return j[key].get<double>();
}

int reqInt(const Json& j, const char* key) {
    if (!j.contains(key) || !j[key].is_number_integer())
        throw ProtocolError{400, "INVALID_REQUEST",
                            std::string("missing/invalid integer '") + key + "'"};
    return j[key].get<int>();
}

double optDouble(const Json& j, const char* key, double dflt) {
    return j.value(key, dflt);
}

}  // namespace

Json configJson(const MapConfig& cfg) {
    return Json{
        {"width", cfg.width},
        {"height", cfg.height},
        {"resolution", cfg.resolution},
        {"origin", Json::array({cfg.origin_x, cfg.origin_y})},
    };
}

Json paramsJson(const FusionParams& p) {
    return Json{
        {"l_free", p.l_free},
        {"l_hit", p.l_hit},
        {"l_max", p.l_max},
        {"p_free", OccupancyGrid::sigmoid(p.l_free)},
        {"p_hit", OccupancyGrid::sigmoid(p.l_hit)},
    };
}

Json versionJson(const GridVersion& v) {
    return Json{
        {"seq", v.seq},
        {"created_unix", v.created_unix},
        {"payload_digest", v.payload_digest},
        {"state_digest", v.state_digest},
        {"version_digest", v.version_digest},
        {"updates",
         Json{{"free_touches", v.counts.free_cells},
              {"occupied_touches", v.counts.occupied_cells}}},
    };
}

Json mapSummaryJson(const MapRecord& rec) {
    return Json{
        {"id", rec.id},
        {"created_unix", rec.created_unix},
        {"config", configJson(rec.config)},
        {"params", paramsJson(rec.params)},
        {"latest_seq", rec.versions.back().seq},
        {"latest_version_digest", rec.versions.back().version_digest},
    };
}

Json gridExportJson(const MapRecord& rec, const GridVersion& v) {
    const int w = rec.config.width;
    const int h = rec.config.height;
    // Dense rows, outer index = iy (origin row first). Unknown => null;
    // observed => probability; also include raw log-odds and counters.
    Json rows = Json::array();
    for (int iy = 0; iy < h; ++iy) {
        Json row = Json::array();
        for (int ix = 0; ix < w; ++ix) {
            int k = iy * w + ix;
            double lo = v.snapshot.log_odds[k];
            bool observed = v.snapshot.observed[k] != 0;
            Json cell;
            if (!observed) {
                cell = Json{{"state", "unknown"}, {"p", nullptr}};
            } else {
                // Distinguish unknown from an observed cell at the 0.5 prior.
                cell = Json{{"state", "observed"},
                            {"p", OccupancyGrid::sigmoid(lo)},
                            {"log_odds", lo}};
            }
            row.push_back(cell);
        }
        rows.push_back(row);
    }
    Json counts_free = Json::array();
    Json counts_hit = Json::array();
    for (int iy = 0; iy < h; ++iy) {
        Json rf = Json::array();
        Json rh = Json::array();
        for (int ix = 0; ix < w; ++ix) {
            int k = iy * w + ix;
            rf.push_back(v.snapshot.free_touches[k]);
            rh.push_back(v.snapshot.hit_touches[k]);
        }
        counts_free.push_back(rf);
        counts_hit.push_back(rh);
    }

    return Json{
        {"map_id", rec.id},
        {"seq", v.seq},
        {"state_digest", v.state_digest},
        {"version_digest", v.version_digest},
        {"config", configJson(rec.config)},
        {"grid",
         Json{{"ordering", "row-major"},
              {"probability_prior", 0.5},
              {"cells", rows},
              {"free_touch_counts", counts_free},
              {"occupied_touch_counts", counts_hit}}},
    };
}

Json versionDetailJson(const MapRecord& rec, const GridVersion& v) {
    Json j = versionJson(v);
    j["config"] = configJson(rec.config);
    return j;
}

CreateMapOptions parseCreateMap(const Json& body) {
    if (!body.is_object())
        throw ProtocolError{400, "INVALID_REQUEST", "body must be a JSON object"};
    CreateMapOptions o;
    o.width = reqInt(body, "width");
    o.height = reqInt(body, "height");
    o.resolution = optDouble(body, "resolution", 0.1);
    if (body.contains("origin")) {
        const auto& org = body["origin"];
        if (!org.is_array() || org.size() != 2 ||
            !org[0].is_number() || !org[1].is_number())
            throw ProtocolError{400, "INVALID_REQUEST",
                                "'origin' must be [x, y] numbers"};
        o.origin_x = org[0].get<double>();
        o.origin_y = org[1].get<double>();
    }
    o.p_hit = optDouble(body, "p_hit", 0.6);
    o.p_free = optDouble(body, "p_free", 0.4);
    o.l_max = optDouble(body, "l_max", 3.0);
    return o;
}

ParsedScan parseScan(const Json& body) {
    if (!body.is_object())
        throw ProtocolError{400, "INVALID_REQUEST", "body must be a JSON object"};
    ParsedScan out;

    Pose pose;
    if (body.contains("pose")) {
        const auto& ps = body["pose"];
        if (ps.is_array() && ps.size() == 3) {
            pose.x = ps[0].get<double>();
            pose.y = ps[1].get<double>();
            pose.theta = ps[2].get<double>();
        } else if (ps.is_object()) {
            pose.x = reqDouble(ps, "x");
            pose.y = reqDouble(ps, "y");
            pose.theta = reqDouble(ps, "theta");
        } else {
            throw ProtocolError{400, "INVALID_REQUEST",
                                "'pose' must be [x,y,theta] or object"};
        }
    }
    out.scan.pose = pose;

    // Geometry echo used to reject mixing into an incompatible grid.
    if (body.contains("resolution")) {
        if (!body["resolution"].is_number())
            throw ProtocolError{400, "INVALID_REQUEST", "bad resolution"};
        out.has_resolution = true;
        out.resolution = body["resolution"].get<double>();
    }
    if (body.contains("origin")) {
        // Top-level "origin" is reserved: pose lives in "pose", the grid
        // origin echo lives in "grid_origin". Reject ambiguity explicitly.
        throw ProtocolError{
            400, "INVALID_REQUEST",
            "top-level 'origin' is not accepted on scans: use 'pose' for the "
            "sensor pose and 'grid_origin' to echo the map origin"};
    }
    if (body.contains("grid_origin")) {
        const auto& org = body["grid_origin"];
        if (!org.is_array() || org.size() != 2 || !org[0].is_number() ||
            !org[1].is_number())
            throw ProtocolError{400, "INVALID_REQUEST",
                                "'grid_origin' must be [x, y]"};
        out.has_origin = true;
        out.origin_x = org[0].get<double>();
        out.origin_y = org[1].get<double>();
    }
    if (body.contains("base_seq")) {
        out.has_base_seq = true;
        out.base_seq = body["base_seq"].get<std::uint64_t>();
    }

    std::vector<Beam> beams;

    // Preferred form: {"returns":[{"angle":..,"range":..}], "max_range":..}
    if (body.contains("returns")) {
        const auto& ret = body["returns"];
        if (!ret.is_array())
            throw ProtocolError{400, "INVALID_REQUEST", "'returns' must be array"};
        double max_range = reqDouble(body, "max_range");
        for (const auto& r : ret) {
            if (!r.is_object() && !r.is_array())
                throw ProtocolError{400, "INVALID_REQUEST",
                                    "each return must be [angle,range] or object"};
            SensorReturn s;
            if (r.is_array()) {
                if (r.size() != 2)
                    throw ProtocolError{400, "INVALID_REQUEST",
                                        "return tuple must be [angle, range]"};
                s.angle = r[0].get<double>();
                s.range = r[1].get<double>();
            } else {
                s.angle = reqDouble(r, "angle");
                s.range = reqDouble(r, "range");
            }
            beams.push_back(beamFromReturn(pose, s, max_range));
        }
    } else if (body.contains("beams")) {
        const auto& arr = body["beams"];
        if (!arr.is_array())
            throw ProtocolError{400, "INVALID_REQUEST", "'beams' must be array"};
        for (const auto& bj : arr) {
            Beam b;
            if (bj.is_array()) {
                // [ox,oy,ex,ey] hit, or [ox,oy,ex,ey,false,max] no-return
                if (bj.size() < 4)
                    throw ProtocolError{400, "INVALID_REQUEST",
                                        "beam tuple needs [ox,oy,ex,ey,...]"};
                b.ox = bj[0].get<double>();
                b.oy = bj[1].get<double>();
                b.ex = bj[2].get<double>();
                b.ey = bj[3].get<double>();
                if (bj.size() >= 5) b.hit = bj[4].get<bool>();
                if (bj.size() >= 6) b.max_range = bj[5].get<double>();
            } else if (bj.is_object()) {
                b.ox = reqDouble(bj, "ox");
                b.oy = reqDouble(bj, "oy");
                b.ex = reqDouble(bj, "ex");
                b.ey = reqDouble(bj, "ey");
                b.hit = bj.value("hit", true);
                b.max_range = bj.value("max_range", 0.0);
            } else {
                throw ProtocolError{400, "INVALID_REQUEST", "bad beam entry"};
            }
            if (!b.hit && !(b.max_range > 0.0))
                throw ProtocolError{400, "INVALID_REQUEST",
                                    "no-return beam requires max_range"};
            beams.push_back(b);
        }
    } else {
        throw ProtocolError{400, "INVALID_REQUEST",
                            "scan requires 'returns' or 'beams'"};
    }

    out.scan.beams = std::move(beams);
    return out;
}

// --- target canonicalization -------------------------------------------------

namespace {

std::vector<std::string> split(const std::string& s, char sep) {
    std::vector<std::string> out;
    std::string cur;
    for (char c : s) {
        if (c == sep) {
            out.push_back(cur);
            cur.clear();
        } else {
            cur.push_back(c);
        }
    }
    out.push_back(cur);
    return out;
}

}  // namespace

std::string canonicalizeTarget(const std::string& target) {
    auto qpos = target.find('?');
    if (qpos == std::string::npos) return target;
    std::string path = target.substr(0, qpos);
    std::string query = target.substr(qpos + 1);
    auto pairs = split(query, '&');
    std::vector<std::string> nonempty;
    for (auto& p : pairs)
        if (!p.empty()) nonempty.push_back(p);
    std::sort(nonempty.begin(), nonempty.end());
    std::string out = path;
    if (!nonempty.empty()) {
        out.push_back('?');
        for (size_t i = 0; i < nonempty.size(); ++i) {
            if (i) out.push_back('&');
            out += nonempty[i];
        }
    }
    return out;
}

std::string canonicalRequest(const std::string& method,
                             const std::string& target,
                             std::int64_t timestamp,
                             const std::string& nonce,
                             const std::string& body_sha256_hex) {
    std::ostringstream os;
    std::string m = method;
    for (char& c : m) c = static_cast<char>(std::toupper(c));
    os << "SIGNED-REQUEST:v1\n"
       << "method=" << m << '\n'
       << "target=" << canonicalizeTarget(target) << '\n'
       << "timestamp=" << timestamp << '\n'
       << "nonce=" << nonce << '\n'
       << "body_sha256=" << body_sha256_hex << '\n';
    return os.str();
}

Json errorBody(const std::string& code, const std::string& message) {
    return Json{{"error", Json{{"code", code}, {"message", message}}}};
}

}  // namespace pf
