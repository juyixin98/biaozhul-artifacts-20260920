// api.cpp
#include "api.hpp"

#include "../third_party/json.hpp"
#include "sha256.hpp"

#include <cmath>
#include <string>

using nlohmann::json;

namespace api {

namespace {

tftree::Error badRequest(const std::string& msg) {
    return tftree::Error("bad_request", msg, 400);
}

std::string requireString(const json& j, const char* key) {
    if (!j.contains(key) || !j.at(key).is_string())
        throw badRequest(std::string("missing or non-string field: ") + key);
    return j.at(key).get<std::string>();
}

double requireNumber(const json& j, const char* key) {
    if (!j.contains(key) || !j.at(key).is_number())
        throw badRequest(std::string("missing or non-number field: ") + key);
    return j.at(key).get<double>();
}

// 四元数接受 [w,x,y,z] 或 {"w":..,"x":..,"y":..,"z":..}
Eigen::Quaterniond parseQuaternion(const json& j) {
    if (j.is_array() && j.size() == 4) {
        return Eigen::Quaterniond(j[0].get<double>(), j[1].get<double>(),
                                  j[2].get<double>(), j[3].get<double>());
    }
    if (j.is_object())
        return Eigen::Quaterniond(requireNumber(j, "w"), requireNumber(j, "x"),
                                  requireNumber(j, "y"), requireNumber(j, "z"));
    throw badRequest(
        "quaternion must be [w,x,y,z] or {w,x,y,z}");
}

Eigen::Vector3d parseTranslation(const json& j) {
    if (!j.is_array() || j.size() != 3)
        throw badRequest("translation must be [x,y,z]");
    return Eigen::Vector3d(j[0].get<double>(), j[1].get<double>(),
                           j[2].get<double>());
}

json quatJson(const Eigen::Quaterniond& qIn) {
    Eigen::Quaterniond q = tfmath::canonical(qIn);
    return json{{"w", q.w()}, {"x", q.x()}, {"y", q.y()}, {"z", q.z()}};
}

json matrixJson(const Eigen::Matrix4d& m) {
    json rows = json::array();
    for (int r = 0; r < 4; ++r) {
        json row = json::array();
        for (int c = 0; c < 4; ++c) row.push_back(m(r, c));
        rows.push_back(row);
    }
    return rows;
}

json edgeJson(const tftree::EdgeSample& e) {
    json samples = json::array();
    for (double t : e.sample_times) samples.push_back(t);
    return json{
        {"parent", e.parent},
        {"child", e.child},
        {"direction", e.inverted ? "child->parent (inverted)"
                                  : "parent->child"},
        {"edge_type", e.is_static ? "static" : "dynamic"},
        {"mode", e.mode},
        {"requested_time", e.requested_time},
        {"sample_times_used", samples},
        {"time_error", e.time_error},
    };
}

}  // namespace

http::Response App::handle(const http::Request& req) {
    auto reply = [&](int status, json body) -> http::Response {
        http::Response r;
        r.status = status;
        r.content_type = "application/json; charset=utf-8";
        r.body = body.dump();
        return r;
    };

    try {
        if (req.method == "GET" && req.path == "/health") {
            return reply(200, {{"status", "ok"}, {"service", "tf-tree"}});
        }

        if (req.method == "GET" && req.path == "/frames") {
            json arr = json::array();
            for (const auto& f : tree_.listFrames()) {
                json entry = {
                    {"name", f.name},
                    {"parent", f.parent.empty() ? json(nullptr)
                                                : json(f.parent)},
                    {"sample_count", f.sample_count},
                };
                if (f.parent.empty())
                    entry["edge_type"] = nullptr;
                else
                    entry["edge_type"] = f.is_static ? "static" : "dynamic";
                if (f.is_static || f.sample_count == 0)
                    entry["time_range"] = nullptr;
                else
                    entry["time_range"] = {{"begin", f.t_min},
                                           {"end", f.t_max}};
                arr.push_back(std::move(entry));
            }
            return reply(200, {{"frames", arr}});
        }

        if (req.method == "POST" && req.path == "/edges/static") {
            json j;
            try {
                j = json::parse(req.body.empty() ? "{}" : req.body);
            } catch (const std::exception&) {
                throw badRequest("invalid JSON body");
            }
            std::string parent = requireString(j, "parent_frame");
            std::string child = requireString(j, "child_frame");
            Eigen::Vector3d t = parseTranslation(j.value("translation",
                                                         json::array({0, 0, 0})));
            // 默认单位四元数：显式给出 identity 或 rotation。
            Eigen::Quaterniond q = Eigen::Quaterniond::Identity();
            if (j.contains("rotation")) q = parseQuaternion(j.at("rotation"));

            tree_.addStaticEdge(parent, child, t, q);
            return reply(200, {{"accepted", true},
                               {"edge_type", "static"},
                               {"parent_frame", parent},
                               {"child_frame", child}});
        }

        if (req.method == "POST" && req.path == "/edges/dynamic") {
            json j;
            try {
                j = json::parse(req.body.empty() ? "{}" : req.body);
            } catch (const std::exception&) {
                throw badRequest("invalid JSON body");
            }
            std::string parent = requireString(j, "parent_frame");
            std::string child = requireString(j, "child_frame");
            if (!j.contains("samples") || !j.at("samples").is_array())
                throw badRequest("missing array field: samples");

            std::size_t n = 0;
            for (const auto& sj : j.at("samples")) {
                tfmath::Sample s;
                s.t = requireNumber(sj, "time");
                s.p = parseTranslation(
                    sj.value("translation", json::array({0, 0, 0})));
                s.q = sj.contains("rotation")
                          ? parseQuaternion(sj.at("rotation"))
                          : Eigen::Quaterniond::Identity();
                tree_.addDynamicSample(parent, child, s);
                ++n;
            }
            return reply(200, {{"accepted", n},
                               {"edge_type", "dynamic"},
                               {"parent_frame", parent},
                               {"child_frame", child}});
        }

        if (req.method == "POST" && req.path == "/query") {
            json j;
            try {
                j = json::parse(req.body.empty() ? "{}" : req.body);
            } catch (const std::exception&) {
                throw badRequest("invalid JSON body");
            }
            std::string source = requireString(j, "source_frame");
            std::string target = requireString(j, "target_frame");
            double time = requireNumber(j, "time");
            double tol = j.value("boundary_tolerance_seconds", 0.0);
            if (!(tol >= 0.0)) throw badRequest("bad tolerance");

            tftree::QueryResult r =
                tree_.query(source, target, time, tol);
            json edges = json::array();
            for (const auto& e : r.edges) edges.push_back(edgeJson(e));
            return reply(200, {
                {"source_frame", source},
                {"target_frame", target},
                {"time", time},
                {"translation", {r.translation.x(), r.translation.y(),
                                 r.translation.z()}},
                {"rotation", quatJson(r.rotation)},
                {"matrix", matrixJson(r.matrix)},
                {"max_time_error", r.max_time_error},
                {"edges_used", edges},
            });
        }

        if (req.method == "POST" && req.path == "/validate/quaternion") {
            json j;
            try {
                j = json::parse(req.body.empty() ? "{}" : req.body);
            } catch (const std::exception&) {
                throw badRequest("invalid JSON body");
            }
            if (!j.contains("rotation")) throw badRequest("missing rotation");
            Eigen::Quaterniond q = parseQuaternion(j.at("rotation"));
            tfmath::QuatCheck c = tfmath::checkQuaternion(q);
            return reply(200, {
                {"valid", c.valid},
                {"unit_length", c.normalized},
                {"norm", c.norm},
                {"reason", c.reason},
                {"normalized", c.valid && !c.normalized
                                   ? quatJson(tfmath::validatedQuaternion(q))
                                   : nullptr},
            });
        }

        if (req.path == "/health" || req.path == "/frames" ||
            req.path == "/edges/static" || req.path == "/edges/dynamic" ||
            req.path == "/query" || req.path == "/validate/quaternion") {
            return reply(405, {{"error", "method_not_allowed"}});
        }
        return reply(404, {{"error", "not_found"}, {"path", req.path}});

    } catch (const tftree::Error& e) {
        return reply(e.http_status,
                     {{"error", e.code}, {"message", e.what()}});
    } catch (const std::invalid_argument& e) {
        return reply(400, {{"error", "bad_request"}, {"message", e.what()}});
    } catch (const std::exception& e) {
        return reply(400, {{"error", "bad_request"}, {"message", e.what()}});
    }
}

}  // namespace api
