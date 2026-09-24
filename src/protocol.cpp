#include "protocol.hpp"

#include <cmath>
#include <string>
#include <vector>

#include <Eigen/Dense>
#include <nlohmann/json.hpp>

#include "icp.hpp"
#include "sha256.hpp"

namespace pcrs {

using json = nlohmann::json;

namespace {

HttpResponse makeError(int status, const std::string& code,
                       const std::string& message) {
    json j;
    j["error"] = {{"code", code}, {"message", message}};
    return {status, "application/json", j.dump()};
}

// Extracts a 3D point array. Accepts either a bare JSON array of [x,y,z]
// triples, or an object {"points": [...]}. Throws std::runtime_error with a
// user-facing message on malformed input.
std::vector<Eigen::Vector3d> parseCloud(const json& v, const char* name,
                                        size_t& dropped) {
    const json* arr = &v;
    if (v.is_object()) {
        auto it = v.find("points");
        if (it == v.end())
            throw std::runtime_error(std::string("'") + name +
                                     "' object must contain a 'points' array");
        arr = &*it;
    }
    if (!arr->is_array())
        throw std::runtime_error(std::string("'") + name +
                                 "' must be an array of [x, y, z] triples");
    if (arr->size() > 5000)
        throw std::runtime_error(std::string("'") + name +
                                 "' exceeds the 5000 point limit");
    std::vector<Eigen::Vector3d> pts;
    pts.reserve(arr->size());
    for (size_t i = 0; i < arr->size(); ++i) {
        const json& p = (*arr)[i];
        if (!p.is_array() || p.size() != 3)
            throw std::runtime_error(
                std::string("'") + name +
                "' contains a point that is not a length-3 numeric array");
        Eigen::Vector3d q;
        for (int k = 0; k < 3; ++k) {
            if (!p[k].is_number())
                throw std::runtime_error(
                    std::string("non-numeric coordinate in '") + name + "'");
            q[k] = p[k].get<double>();
        }
        if (std::isfinite(q.x()) && std::isfinite(q.y()) &&
            std::isfinite(q.z())) {
            pts.push_back(q);
        } else {
            ++dropped;
        }
    }
    return pts;
}

json vec3ToJson(const Eigen::Vector3d& v) {
    return {v.x(), v.y(), v.z()};
}

json mat3ToJson(const Eigen::Matrix3d& m) {
    json out = json::array();
    for (int i = 0; i < 3; ++i) {
        json row = json::array();
        for (int j = 0; j < 3; ++j) row.push_back(m(i, j));
        out.push_back(row);
    }
    return out;
}

}  // namespace

HttpResponse handleRequest(const std::string& method, const std::string& path,
                           const std::string& body) {
    if (method == "GET" && (path == "/health" || path == "/healthz")) {
        json j;
        j["status"] = "ok";
        j["service"] = "point-cloud-registration";
        j["algorithm"] = "point-to-point ICP";
        return {200, "application/json", j.dump()};
    }

    if (path != "/register") {
        return makeError(404, "not_found",
                         "Unknown route. Use POST /register or GET /health.");
    }
    if (method != "POST") {
        return makeError(405, "method_not_allowed",
                         "Use POST for /register.");
    }

    json req;
    try {
        req = json::parse(body);
    } catch (const json::parse_error& e) {
        return makeError(400, "invalid_json",
                         std::string("Request body is not valid JSON: ") +
                             e.what());
    }
    if (!req.is_object())
        return makeError(400, "invalid_request",
                         "Request body must be a JSON object.");
    if (!req.contains("source") || !req.contains("target"))
        return makeError(400, "missing_fields",
                         "Both 'source' and 'target' point clouds are "
                         "required.");

    size_t srcDropped = 0, tgtDropped = 0;
    std::vector<Eigen::Vector3d> source, target;
    try {
        source = parseCloud(req["source"], "source", srcDropped);
        target = parseCloud(req["target"], "target", tgtDropped);
    } catch (const std::runtime_error& e) {
        return makeError(400, "invalid_cloud", e.what());
    }

    IcpOptions opts;
    if (req.contains("max_iterations")) {
        const json& v = req["max_iterations"];
        if (!v.is_number_integer() || v.get<int>() < 1 ||
            v.get<int>() > 10000)
            return makeError(400, "invalid_option",
                             "'max_iterations' must be an integer in [1,10000].");
        opts.max_iterations = v.get<int>();
    }
    if (req.contains("max_correspondence_distance")) {
        const json& v = req["max_correspondence_distance"];
        if (!v.is_number())
            return makeError(400, "invalid_option",
                             "'max_correspondence_distance' must be numeric "
                             "(omit for automatic).");
        opts.max_correspondence_distance = v.get<double>();
        if (std::isnan(opts.max_correspondence_distance))
            return makeError(400, "invalid_option",
                             "'max_correspondence_distance' must be finite.");
        if (opts.max_correspondence_distance != 0.0 &&
            opts.max_correspondence_distance <= 0.0)
            return makeError(400, "invalid_option",
                             "'max_correspondence_distance' must be positive "
                             "or 0 (automatic).");
    }
    if (req.contains("transformation_epsilon")) {
        const json& v = req["transformation_epsilon"];
        if (!v.is_number() || v.get<double>() <= 0.0)
            return makeError(400, "invalid_option",
                             "'transformation_epsilon' must be a positive "
                             "number.");
        opts.transformation_epsilon = v.get<double>();
    }
    if (req.contains("fitness_epsilon")) {
        const json& v = req["fitness_epsilon"];
        if (!v.is_number() || v.get<double>() <= 0.0)
            return makeError(400, "invalid_option",
                             "'fitness_epsilon' must be a positive number.");
        opts.fitness_epsilon = v.get<double>();
    }

    if (req.contains("initial_pose")) {
        const json& ip = req["initial_pose"];
        if (!ip.is_object() || !ip.contains("rotation") ||
            !ip.contains("translation"))
            return makeError(400, "invalid_initial_pose",
                             "'initial_pose' must be an object with 'rotation' "
                             "(3x3) and 'translation' (length 3).");
        try {
            const json& Rj = ip["rotation"];
            const json& tj = ip["translation"];
            if (!Rj.is_array() || Rj.size() != 3)
                throw std::runtime_error("rotation must be 3x3");
            for (int i = 0; i < 3; ++i) {
                if (!Rj[i].is_array() || Rj[i].size() != 3)
                    throw std::runtime_error("rotation must be 3x3");
                for (int j = 0; j < 3; ++j) {
                    if (!Rj[i][j].is_number())
                        throw std::runtime_error("rotation entries numeric");
                    opts.initial_R(i, j) = Rj[i][j].get<double>();
                }
            }
            if (!tj.is_array() || tj.size() != 3)
                throw std::runtime_error("translation length 3");
            for (int i = 0; i < 3; ++i) {
                if (!tj[i].is_number())
                    throw std::runtime_error("translation entries numeric");
                opts.initial_t(i) = tj[i].get<double>();
            }
        } catch (const std::runtime_error&) {
            return makeError(400, "invalid_initial_pose",
                             "Malformed 'initial_pose'.");
        }
        if (!opts.initial_R.allFinite() || !opts.initial_t.allFinite())
            return makeError(400, "invalid_initial_pose",
                             "'initial_pose' contains non-finite values.");
        const double orthErr =
            (opts.initial_R.transpose() * opts.initial_R -
             Eigen::Matrix3d::Identity())
                .norm();
        if (orthErr > 1e-6)
            return makeError(400, "invalid_initial_pose",
                             "'initial_pose.rotation' is not orthogonal "
                             "(R^T R != I).");
        if (std::fabs(opts.initial_R.determinant() - 1.0) > 1e-6)
            return makeError(400, "invalid_initial_pose",
                             "'initial_pose.rotation' determinant is not 1 "
                             "(reflections are not allowed).");
        opts.has_initial_guess = true;
    }

    const IcpResult res = runIcp(source, target, opts);

    json out;
    out["rotation"] = mat3ToJson(res.R);
    out["translation"] = vec3ToJson(res.t);
    if (std::isfinite(res.rmse))
        out["residual_rmse"] = res.rmse;
    else
        out["residual_rmse"] = nullptr;
    out["inlier_ratio"] = res.inlier_ratio;
    out["num_correspondences"] = res.num_correspondences;
    out["iterations"] = res.iterations;
    out["converged"] = res.converged;
    out["degenerate"] = res.degenerate;
    out["high_confidence"] = res.high_confidence;
    out["reason"] = res.reason;
    out["reason_description"] = reasonDescription(res.reason);
    out["max_correspondence_distance"] =
        res.max_correspondence_distance_used;

    const double rotTol = 1e-10;
    const bool rotationValid =
        res.rotation_orthogonality_error < rotTol &&
        std::fabs(res.rotation_determinant - 1.0) < rotTol;
    out["rotation_check"] = {
        {"orthogonality_error", res.rotation_orthogonality_error},
        {"determinant", res.rotation_determinant},
        {"orthogonal_and_det1", rotationValid}};

    out["diagnostics"] = {
        {"matched_spread_eigenvalues",
         {res.matched_spread_eigenvalues[0],
          res.matched_spread_eigenvalues[1],
          res.matched_spread_eigenvalues[2]}},
        {"cross_covariance_singular_values",
         {res.cross_covariance_singular_values[0],
          res.cross_covariance_singular_values[1],
          res.cross_covariance_singular_values[2]}}};

    out["points"] = {
        {"source_total", res.source_points_total},
        {"source_valid", res.source_points_valid},
        {"source_nonfinite_dropped", srcDropped},
        {"target_total", res.target_points_total},
        {"target_valid", res.target_points_valid},
        {"target_nonfinite_dropped", tgtDropped}};

    out["request_sha256"] = Sha256::hex(body);

    return {200, "application/json", out.dump()};
}

}  // namespace pcrs
