// 球面距离查询服务 —— JSON 请求入口（纯后端，无地图/无前端）。
//
// 用法：
//   sphere_dist <request.json>     # 从文件读取请求
//   sphere_dist                    # 从 stdin 读取请求
//
// 支持两类请求：
//   {"action":"distance", "a":{"lat":..,"lon":..}, "b":{"lat":..,"lon":..}}
//   {"action":"range", "center":{..}, "radius_m":<米>,
//    "points":[{"lat":..,"lon":..}, ...]}
//
// 成功时 stdout 输出 JSON 结果，退出码 0；
// 失败时 stdout 输出 {"ok":false,"error":"..."}，退出码 1。

#include <cmath>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <sstream>
#include <string>

#include "geo.h"
#include "json.h"

namespace {

// 将字符串按 JSON 字符串规则转义（内容可能来自错误信息回显）。
std::string escapeJson(const std::string& in) {
    std::string out;
    out.reserve(in.size() + 2);
    for (unsigned char c : in) {
        switch (c) {
            case '"': out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\b': out += "\\b"; break;
            case '\f': out += "\\f"; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            default:
                if (c < 0x20) {
                    char buf[8];
                    std::snprintf(buf, sizeof(buf), "\\u%04x", c);
                    out += buf;
                } else {
                    out.push_back(static_cast<char>(c));
                }
        }
    }
    return out;
}

// 坐标回显用 12 位有效数字（约 0.01 毫米量级的角度表示），距离用 6 位小数（微米）。
std::string fmtCoord(long double v) {
    std::ostringstream os;
    os << std::setprecision(12) << v;
    return os.str();
}

std::string fmtMeters(long double v) {
    std::ostringstream os;
    os << std::fixed << std::setprecision(6) << v;
    return os.str();
}

std::string pointJson(const geo::LatLon& p) {
    return std::string("{\"lat\":") + fmtCoord(p.lat) +
           ",\"lon\":" + fmtCoord(p.lon) + "}";
}

[[noreturn]] void fail(const std::string& msg, int code = 1) {
    std::cout << "{\"ok\": false, \"error\": \"" << escapeJson(msg) << "\"}\n";
    std::exit(code);
}

const json::Value* requireField(const json::Value& obj,
                                const std::string& key,
                                json::Type type) {
    const json::Value* v = obj.find(key);
    if (!v) fail("missing field: " + key);
    if (!v->is(type)) fail("field has wrong type: " + key);
    return v;
}

geo::LatLon parsePoint(const json::Value& v) {
    if (!v.is(json::Type::Object)) fail("point must be an object {lat, lon}");
    const json::Value* lat = v.find("lat");
    const json::Value* lon = v.find("lon");
    if (!lat || !lat->is(json::Type::Number)) fail("point.lat must be a number");
    if (!lon || !lon->is(json::Type::Number)) fail("point.lon must be a number");
    geo::LatLon p{lat->number, lon->number};
    std::string err;
    if (!geo::normalize(p, &err)) fail(err);
    return p;
}

void handleDistance(const json::Value& req) {
    const json::Value* a = requireField(req, "a", json::Type::Object);
    const json::Value* b = requireField(req, "b", json::Type::Object);
    geo::LatLon pa = parsePoint(*a);
    geo::LatLon pb = parsePoint(*b);

    const long double angle = geo::centralAngle(pa, pb);
    const long double meters = geo::distanceM(pa, pb);

    std::cout
        << "{\n"
        << "  \"ok\": true,\n"
        << "  \"action\": \"distance\",\n"
        << "  \"coordinate_system\": \"EPSG:4326 latitude/longitude in degrees, sphere R=6371000 m\",\n"
        << "  \"a\": " << pointJson(pa) << ",\n"
        << "  \"b\": " << pointJson(pb) << ",\n"
        << "  \"central_angle_rad\": " << std::setprecision(18) << angle << ",\n"
        << "  \"central_angle_deg\": " << std::setprecision(15)
        << (angle * 180.0L / 3.141592653589793238462643383279502884L) << ",\n"
        << "  \"distance_m\": " << fmtMeters(meters) << ",\n"
        << "  \"distance_km\": " << fmtMeters(meters / 1000.0L) << "\n"
        << "}\n";
}

void handleRange(const json::Value& req) {
    const json::Value* center = requireField(req, "center", json::Type::Object);
    const json::Value* radius = requireField(req, "radius_m", json::Type::Number);
    const json::Value* pts = requireField(req, "points", json::Type::Array);

    geo::LatLon pc = parsePoint(*center);
    if (!std::isfinite(radius->number) || radius->number < 0.0L) {
        fail("radius_m must be a non-negative finite number");
    }

    std::vector<geo::LatLon> points;
    points.reserve(pts->arr.size());
    for (std::size_t i = 0; i < pts->arr.size(); ++i) {
        points.push_back(parsePoint(pts->arr[i]));
    }

    std::size_t candidateCount = 0;
    std::vector<geo::Hit> hits =
        geo::rangeQuery(pc, radius->number, points, &candidateCount);

    std::cout
        << "{\n"
        << "  \"ok\": true,\n"
        << "  \"action\": \"range\",\n"
        << "  \"coordinate_system\": \"EPSG:4326 latitude/longitude in degrees, sphere R=6371000 m\",\n"
        << "  \"center\": " << pointJson(pc) << ",\n"
        << "  \"radius_m\": " << fmtMeters(radius->number) << ",\n"
        << "  \"total_points\": " << points.size() << ",\n"
        << "  \"candidate_count\": " << candidateCount << ",\n"
        << "  \"hit_count\": " << hits.size() << ",\n"
        << "  \"hits\": [\n";
    for (std::size_t i = 0; i < hits.size(); ++i) {
        const geo::Hit& h = hits[i];
        std::cout << "    {\"index\": " << h.index
                  << ", \"lat\": " << fmtCoord(h.point.lat)
                  << ", \"lon\": " << fmtCoord(h.point.lon)
                  << ", \"distance_m\": " << fmtMeters(h.distance_m) << "}"
                  << (i + 1 == hits.size() ? "" : ",") << "\n";
    }
    std::cout << "  ]\n}\n";
}

} // namespace

int main(int argc, char** argv) {
    std::string text;
    if (argc > 2) {
        std::cerr << "usage: " << argv[0] << " [request.json]\n";
        return 2;
    }
    if (argc == 2) {
        std::ifstream in(argv[1]);
        if (!in) fail("cannot open input file: " + std::string(argv[1]));
        std::ostringstream ss;
        ss << in.rdbuf();
        text = ss.str();
    } else {
        std::ostringstream ss;
        ss << std::cin.rdbuf();
        text = ss.str();
    }

    std::string err;
    std::unique_ptr<json::Value> root = json::parse(text, &err);
    if (!root) fail("invalid JSON: " + err);
    if (!root->is(json::Type::Object)) fail("request must be a JSON object");

    const json::Value* action = root->find("action");
    if (!action || !action->is(json::Type::String)) fail("missing string field: action");

    if (action->str == "distance") {
        handleDistance(*root);
    } else if (action->str == "range") {
        handleRange(*root);
    } else {
        fail("unknown action: " + action->str + " (expected 'distance' or 'range')");
    }
    return 0;
}
