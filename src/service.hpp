// service.hpp — request validation, simplification, and validation report.
//
// Request JSON:
//   {
//     "crs": "EPSG:3857",        // optional, informational only, echoed back
//     "tolerance": 1.5,          // required, finite, >= 0
//     "points": [[x, y], ...]    // required, 1..N pairs of finite numbers
//   }
//
// Response JSON (status == "ok"):
//   kept_indices / simplified  — the simplified polyline (endpoints preserved)
//   validation                 — per-point distances from each ORIGINAL point
//                                to the SIMPLIFIED polyline, the maximum, and
//                                whether max <= tolerance (one-directional
//                                bound; see douglas_peucker.hpp for what is
//                                explicitly not claimed).
//
// Errors produce {"status":"error","error":"..."} and a non-zero process exit.
#pragma once

#include <cmath>
#include <string>
#include <vector>

#include "douglas_peucker.hpp"
#include "json.hpp"

namespace simp {

struct Request {
    std::string crs = "cartesian"; // informational; coordinates used as-is
    double tolerance = 0.0;
    std::vector<Point> points;
};

inline Request parse_request(const Json& root) {
    if (root.type != Json::Type::Obj) {
        throw JsonError("request must be a JSON object");
    }
    Request req;

    if (const Json* crs = root.find("crs")) {
        if (crs->type != Json::Type::Str) throw JsonError("\"crs\" must be a string");
        req.crs = crs->str;
    }

    const Json* tol = root.find("tolerance");
    if (!tol) throw JsonError("missing required field \"tolerance\"");
    if (tol->type != Json::Type::Num) throw JsonError("\"tolerance\" must be a number");
    if (!std::isfinite(tol->num)) throw JsonError("\"tolerance\" must be finite");
    if (tol->num < 0.0) throw JsonError("\"tolerance\" must be >= 0");
    req.tolerance = tol->num;

    const Json* pts = root.find("points");
    if (!pts) throw JsonError("missing required field \"points\"");
    if (pts->type != Json::Type::Arr) throw JsonError("\"points\" must be an array");
    if (pts->arr.empty()) throw JsonError("\"points\" must contain at least one point");
    for (std::size_t i = 0; i < pts->arr.size(); ++i) {
        const Json& p = pts->arr[i];
        if (p.type != Json::Type::Arr || p.arr.size() != 2 ||
            p.arr[0].type != Json::Type::Num || p.arr[1].type != Json::Type::Num) {
            throw JsonError("point " + std::to_string(i) + " must be a [x, y] number pair");
        }
        if (!std::isfinite(p.arr[0].num) || !std::isfinite(p.arr[1].num)) {
            throw JsonError("point " + std::to_string(i) + " has a non-finite coordinate");
        }
        req.points.push_back({p.arr[0].num, p.arr[1].num});
    }
    return req;
}

inline Json run_request(const Request& req) {
    const std::vector<std::size_t> kept = douglas_peucker(req.points, req.tolerance);

    std::vector<Point> simplified;
    simplified.reserve(kept.size());
    for (std::size_t idx : kept) simplified.push_back(req.points[idx]);

    // Validation: distance from every ORIGINAL point to the SIMPLIFIED
    // polyline (directed check, original -> simplified).
    Json per_point = Json::make_arr();
    double max_dist = 0.0;
    std::size_t max_idx = 0;
    for (std::size_t i = 0; i < req.points.size(); ++i) {
        const double d = dist_point_polyline(req.points[i], simplified);
        per_point.arr.push_back(Json::make_num(d));
        if (d > max_dist) { // strict: lowest index wins ties, deterministic
            max_dist = d;
            max_idx = i;
        }
    }

    // Exact-arithmetic guarantee is max_dist <= tolerance. Allow a tiny
    // floating-point slack relative to the tolerance scale; the raw numbers
    // are reported alongside so nothing is hidden.
    const double slack = 1e-9 * (req.tolerance > 1.0 ? req.tolerance : 1.0);
    const bool satisfied = max_dist <= req.tolerance + slack;

    Json validation = Json::make_obj();
    validation.set("convention",
                   Json::make_str("directed: distance from each original point to the "
                                  "simplified polyline; NOT a bidirectional Hausdorff bound"));
    validation.set("per_point_distance", per_point);
    validation.set("max_distance", Json::make_num(max_dist));
    validation.set("max_distance_point_index", Json::make_num(static_cast<double>(max_idx)));
    validation.set("bound", Json::make_num(req.tolerance));
    validation.set("bound_satisfied", Json::make_bool(satisfied));

    Json kept_json = Json::make_arr();
    for (std::size_t idx : kept) kept_json.arr.push_back(Json::make_num(static_cast<double>(idx)));

    Json simplified_json = Json::make_arr();
    for (const Point& p : simplified) {
        Json pair = Json::make_arr();
        pair.arr.push_back(Json::make_num(p.x));
        pair.arr.push_back(Json::make_num(p.y));
        simplified_json.arr.push_back(std::move(pair));
    }

    Json out = Json::make_obj();
    out.set("status", Json::make_str("ok"));
    out.set("crs", Json::make_str(req.crs));
    out.set("tolerance", Json::make_num(req.tolerance));
    out.set("input_count", Json::make_num(static_cast<double>(req.points.size())));
    out.set("output_count", Json::make_num(static_cast<double>(kept.size())));
    out.set("kept_indices", std::move(kept_json));
    out.set("simplified", std::move(simplified_json));
    out.set("validation", std::move(validation));
    return out;
}

} // namespace simp
