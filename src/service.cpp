// service.cpp — distance / range 请求处理
#include "service.hpp"

#include <algorithm>
#include <cmath>
#include <fstream>
#include <sstream>

namespace svc {

using json::Value;

namespace {

// 构造错误响应
std::string errorBody(const std::string& code, const std::string& message) {
    Value v = Value::makeObject();
    v.obj["ok"] = Value::makeBool(false);
    v.obj["error"] = Value::makeObject();
    v.obj["error"].obj["code"] = Value::makeString(code);
    v.obj["error"].obj["message"] = Value::makeString(message);
    return json::dump(v);
}

Response err(int status, const std::string& code, const std::string& msg) {
    return {status, errorBody(code, msg)};
}

// 读取 { "lat": x, "lon": y }
bool readLatLon(const Value& v, sph::GeoPoint& out, std::string& msg) {
    if (!v.isObject()) { msg = "expected object with lat/lon"; return false; }
    const Value* lat = v.find("lat");
    const Value* lon = v.find("lon");
    if (!lat || !lon) { msg = "missing 'lat' or 'lon'"; return false; }
    if (!lat->isNumber() || !lon->isNumber()) { msg = "'lat' and 'lon' must be numbers"; return false; }
    long double la = static_cast<long double>(lat->number);
    long double lo = static_cast<long double>(lon->number);
    sph::GeoError ge = sph::validateLatLon(la, lo);
    if (ge != sph::GeoError::Ok) { msg = sph::errorMessage(ge); return false; }
    out.lat = la;
    out.lon = (lo == 180.0L) ? -180.0L : lo;  // ±180 同一经线，归一化
    return true;
}

Value bboxToJson(const sph::BBox& b) {
    Value v = Value::makeObject();
    v.obj["lat_min"] = Value::makeNumber(static_cast<double>(b.lat_min));
    v.obj["lat_max"] = Value::makeNumber(static_cast<double>(b.lat_max));
    v.obj["lon_min"] = Value::makeNumber(static_cast<double>(b.lon_min));
    v.obj["lon_max"] = Value::makeNumber(static_cast<double>(b.lon_max));
    v.obj["crosses_antimeridian"] = Value::makeBool(b.crossesAntimeridian());
    Value parts = Value::makeArray();
    for (const auto& part : b.split()) {
        Value pv = Value::makeObject();
        pv.obj["lat_min"] = Value::makeNumber(static_cast<double>(part.lat_min));
        pv.obj["lat_max"] = Value::makeNumber(static_cast<double>(part.lat_max));
        pv.obj["lon_min"] = Value::makeNumber(static_cast<double>(part.lon_min));
        pv.obj["lon_max"] = Value::makeNumber(static_cast<double>(part.lon_max));
        parts.arr.push_back(pv);
    }
    v.obj["parts"] = parts;
    return v;
}

// ---------------- distance ----------------

Response handleDistance(const Value& req) {
    const Value* a = req.find("a");
    const Value* b = req.find("b");
    if (!a || !b) return err(400, "missing_field", "request needs 'a' and 'b' points");

    sph::GeoPoint pa, pb;
    std::string msg;
    if (!readLatLon(*a, pa, msg)) return err(400, "invalid_point", std::string("a: ") + msg);
    if (!readLatLon(*b, pb, msg)) return err(400, "invalid_point", std::string("b: ") + msg);

    long double d = sph::sphericalDistance(pa, pb);

    Value resp = Value::makeObject();
    resp.obj["ok"] = Value::makeBool(true);
    resp.obj["distance_m"] = Value::makeNumber(static_cast<double>(d));
    resp.obj["distance_km"] = Value::makeNumber(static_cast<double>(d / 1000.0L));
    resp.obj["unit"] = Value::makeString("meter");
    resp.obj["earth_radius_m"] = Value::makeNumber(static_cast<double>(sph::EARTH_RADIUS_M));
    return {200, json::dump(resp)};
}

// ---------------- range ----------------

Response handleRange(const Value& req) {
    const Value* center = req.find("center");
    const Value* radius = req.find("radius_m");
    if (!center) return err(400, "missing_field", "request needs 'center'");
    if (!radius) return err(400, "missing_field", "request needs 'radius_m'");
    if (!radius->isNumber() || radius->number < 0)
        return err(400, "invalid_radius", "'radius_m' must be a non-negative number");

    sph::GeoPoint c;
    std::string msg;
    if (!readLatLon(*center, c, msg))
        return err(400, "invalid_point", std::string("center: ") + msg);

    long double radiusM = static_cast<long double>(radius->number);

    // 数据集：优先内联 points，其次 points_file
    std::vector<Point> points;
    if (const Value* pv = req.find("points")) {
        if (!pv->isArray()) return err(400, "invalid_points", "'points' must be an array");
        points.reserve(pv->arr.size());
        for (size_t i = 0; i < pv->arr.size(); ++i) {
            const Value& item = pv->arr[i];
            sph::GeoPoint gp;
            if (!readLatLon(item, gp, msg))
                return err(400, "invalid_point",
                           "points[" + std::to_string(i) + "]: " + msg);
            Point p;
            if (const Value* id = item.find("id"); id && id->isString()) p.id = id->str;
            else p.id = std::to_string(i);
            p.geo = gp;
            points.push_back(std::move(p));
        }
    } else if (const Value* pf = req.find("points_file")) {
        if (!pf->isString()) return err(400, "invalid_points_file", "'points_file' must be a string");
        LoadResult lr = loadPointsFile(pf->str);
        if (!lr.ok) return err(400, "points_file_error", lr.error);
        points = std::move(lr.points);
    } else {
        return err(400, "missing_field", "request needs 'points' or 'points_file'");
    }

    // 第一步：包围盒粗筛（仅候选过滤）
    sph::BBox box;
    sph::sphericalCapBBox(c, radiusM, box);

    long double candidates = 0;
    // 第二步：对候选用精确球面距离复算并过滤
    struct Hit { const Point* p; long double d; };
    std::vector<Hit> hits;
    for (const Point& p : points) {
        if (sph::bboxContains(box, p.geo)) {
            ++candidates;
            long double d = sph::sphericalDistance(c, p.geo);
            if (d <= radiusM) hits.push_back({&p, d});
        }
    }

    std::sort(hits.begin(), hits.end(),
              [](const Hit& x, const Hit& y) {
                  if (x.d != y.d) return x.d < y.d;
                  return x.p->id < y.p->id;  // 距离相同按 id 稳定排序
              });

    Value arr = Value::makeArray();
    for (const Hit& h : hits) {
        Value hv = Value::makeObject();
        hv.obj["id"] = Value::makeString(h.p->id);
        hv.obj["lat"] = Value::makeNumber(static_cast<double>(h.p->geo.lat));
        hv.obj["lon"] = Value::makeNumber(static_cast<double>(h.p->geo.lon));
        hv.obj["distance_m"] = Value::makeNumber(static_cast<double>(h.d));
        arr.arr.push_back(std::move(hv));
    }

    Value resp = Value::makeObject();
    resp.obj["ok"] = Value::makeBool(true);
    resp.obj["center"] = [&] {
        Value cv = Value::makeObject();
        cv.obj["lat"] = Value::makeNumber(static_cast<double>(c.lat));
        cv.obj["lon"] = Value::makeNumber(static_cast<double>(c.lon));
        return cv;
    }();
    resp.obj["radius_m"] = Value::makeNumber(static_cast<double>(radiusM));
    resp.obj["total_points"] = Value::makeNumber(static_cast<double>(points.size()));
    resp.obj["candidate_count"] = Value::makeNumber(static_cast<double>(candidates));
    resp.obj["match_count"] = Value::makeNumber(static_cast<double>(hits.size()));
    resp.obj["candidate_bbox"] = bboxToJson(box);
    resp.obj["matches"] = std::move(arr);
    return {200, json::dump(resp)};
}

}  // namespace

LoadResult loadPointsJson(const std::string& text) {
    LoadResult lr;
    auto parsed = json::parse(text);
    if (!parsed.ok) { lr.error = "JSON parse error: " + parsed.error; return lr; }
    const Value* arr = parsed.value.find("points");
    if (!arr || !arr->isArray()) { lr.error = "top-level 'points' array required"; return lr; }
    lr.points.reserve(arr->arr.size());
    for (size_t i = 0; i < arr->arr.size(); ++i) {
        const Value& item = arr->arr[i];
        sph::GeoPoint gp;
        std::string msg;
        if (!readLatLon(item, gp, msg)) {
            lr.error = "points[" + std::to_string(i) + "]: " + msg;
            return lr;
        }
        Point p;
        if (const Value* id = item.find("id"); id && id->isString()) p.id = id->str;
        else p.id = std::to_string(i);
        p.geo = gp;
        lr.points.push_back(std::move(p));
    }
    lr.ok = true;
    return lr;
}

LoadResult loadPointsFile(const std::string& path) {
    std::ifstream f(path, std::ios::binary);
    if (!f) return {false, "cannot open points file: " + path, {}};
    std::ostringstream ss;
    ss << f.rdbuf();
    if (!f.good() && !f.eof()) return {false, "failed reading points file: " + path, {}};
    return loadPointsJson(ss.str());
}

Response handleRequest(const std::string& requestText) {
    auto parsed = json::parse(requestText);
    if (!parsed.ok)
        return err(400, "invalid_json", "JSON parse error: " + parsed.error);
    const Value& root = parsed.value;
    if (!root.isObject())
        return err(400, "invalid_request", "request must be a JSON object");

    const Value* action = root.find("action");
    if (!action || !action->isString())
        return err(400, "missing_field", "request needs string field 'action'");

    if (action->str == "distance") return handleDistance(root);
    if (action->str == "range") return handleRange(root);
    return err(400, "unknown_action",
               "unknown action '" + action->str + "' (expected 'distance' or 'range')");
}

}  // namespace svc
