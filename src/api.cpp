#include "topp/api.hpp"
#include "topp/planner.hpp"
#include "topp/sha256.hpp"
#include <sstream>

namespace topp {

namespace {

JsonValue errorBody(const std::string& code, const std::string& message) {
    auto o = JsonValue::object();
    o.asObject()["error"] = JsonValue(code);
    o.asObject()["message"] = JsonValue(message);
    return o;
}

int statusFor(ErrorCode c) {
    switch (c) {
        case ErrorCode::BAD_REQUEST: return 400;
        case ErrorCode::INFEASIBLE_LIMITS:
        case ErrorCode::INFEASIBLE_BOUNDARY:
        case ErrorCode::INFEASIBLE_PROFILE: return 422;
        case ErrorCode::INTERNAL_ERROR: return 500;
    }
    return 500;
}

const char* codeName(ErrorCode c) {
    switch (c) {
        case ErrorCode::BAD_REQUEST: return "BAD_REQUEST";
        case ErrorCode::INFEASIBLE_LIMITS: return "INFEASIBLE_LIMITS";
        case ErrorCode::INFEASIBLE_BOUNDARY: return "INFEASIBLE_BOUNDARY";
        case ErrorCode::INFEASIBLE_PROFILE: return "INFEASIBLE_PROFILE";
        case ErrorCode::INTERNAL_ERROR: return "INTERNAL_ERROR";
    }
    return "INTERNAL_ERROR";
}

Eigen::MatrixXd parsePoints(const JsonValue& v, int dof) {
    if (!v.is(JsonValue::Type::Array) || v.asArray().empty())
        throw ApiError(ErrorCode::BAD_REQUEST,
                       "'points' must be a non-empty array");
    int n = static_cast<int>(v.asArray().size());
    Eigen::MatrixXd W(dof, n);
    for (int i = 0; i < n; ++i) {
        const auto& pt = v.asArray()[i];
        if (!pt.is(JsonValue::Type::Array) ||
            static_cast<int>(pt.asArray().size()) != dof)
            throw ApiError(ErrorCode::BAD_REQUEST,
                           "every point must be an array with one value per "
                           "joint");
        for (int j = 0; j < dof; ++j) {
            const auto& c = pt.asArray()[j];
            if (!c.is(JsonValue::Type::Number))
                throw ApiError(ErrorCode::BAD_REQUEST,
                               "point coordinates must be numbers");
            W(j, i) = c.asNumber();
        }
    }
    return W;
}

Eigen::VectorXd parseLimitVector(const JsonValue& parent,
                                 const std::string& key, int dof) {
    if (!parent.contains(key) ||
        !parent.at(key).is(JsonValue::Type::Array))
        throw ApiError(ErrorCode::BAD_REQUEST,
                       "'" + key + "' must be an array");
    const auto& arr = parent.at(key).asArray();
    if (static_cast<int>(arr.size()) != dof)
        throw ApiError(ErrorCode::BAD_REQUEST,
                       "'" + key + "' must have one entry per joint");
    Eigen::VectorXd out(dof);
    for (int j = 0; j < dof; ++j) {
        if (!arr[j].is(JsonValue::Type::Number))
            throw ApiError(ErrorCode::BAD_REQUEST,
                           "'" + key + "' entries must be numbers");
        out(j) = arr[j].asNumber();
    }
    return out;
}

JsonValue vecToJson(const Eigen::VectorXd& v) {
    auto a = JsonValue::array();
    for (int i = 0; i < v.size(); ++i) a.asArray().push_back(JsonValue(v(i)));
    return a;
}

JsonValue buildSuccess(const Result& res, const PlanningRequest& req,
                       const std::string& interpolation,
                       int effectiveGrid) {
    auto root = JsonValue::object();
    auto& o = root.asObject();
    o["duration"] = JsonValue(res.duration);
    o["removed_duplicates"] = JsonValue(res.removedDuplicates);
    o["retained_input_indices"] = [&] {
        auto a = JsonValue::array();
        for (int idx : res.retainedInputIndices)
            a.asArray().push_back(JsonValue(idx));
        return a;
    }();

    auto states = JsonValue::array();
    for (const auto& ps : res.points) {
        auto po = JsonValue::object();
        po.asObject()["t"] = JsonValue(ps.t);
        po.asObject()["q"] = vecToJson(ps.q);
        po.asObject()["qd"] = vecToJson(ps.qd);
        po.asObject()["qdd"] = vecToJson(ps.qdd);
        states.asArray().push_back(std::move(po));
    }
    o["points"] = std::move(states);

    auto intervals = JsonValue::array();
    for (const auto& ai : res.activeIntervals) {
        auto io = JsonValue::object();
        io.asObject()["joint"] = JsonValue(ai.joint);
        io.asObject()["limit"] = JsonValue(ai.limit);
        io.asObject()["u_index_from"] = JsonValue(ai.uIndexFrom);
        io.asObject()["u_index_to"] = JsonValue(ai.uIndexTo);
        io.asObject()["u_from"] = JsonValue(ai.uFrom);
        io.asObject()["u_to"] = JsonValue(ai.uTo);
        io.asObject()["t_from"] = JsonValue(ai.tFrom);
        io.asObject()["t_to"] = JsonValue(ai.tTo);
        io.asObject()["max_ratio"] = JsonValue(ai.maxRatio);
        intervals.asArray().push_back(std::move(io));
    }
    o["active_intervals"] = std::move(intervals);

    o["bottleneck_velocity_joint"] =
        res.bottleneckVelocityJoint < 0 ? JsonValue()
                                        : JsonValue(res.bottleneckVelocityJoint);
    o["bottleneck_acceleration_joint"] =
        res.bottleneckAccelerationJoint < 0
            ? JsonValue()
            : JsonValue(res.bottleneckAccelerationJoint);

    auto vo = JsonValue::object();
    vo.asObject()["passed"] = JsonValue(res.verification.passed);
    vo.asObject()["max_velocity_ratio"] =
        JsonValue(res.verification.maxVelocityRatio);
    vo.asObject()["max_velocity_joint"] =
        JsonValue(res.verification.maxVelocityJoint);
    vo.asObject()["max_acceleration_ratio"] =
        JsonValue(res.verification.maxAccelerationRatio);
    vo.asObject()["max_acceleration_joint"] =
        JsonValue(res.verification.maxAccelerationJoint);
    vo.asObject()["dense_samples"] = JsonValue(res.verification.denseSamples);
    vo.asObject()["detail"] = JsonValue(res.verification.detail);
    o["verification"] = std::move(vo);

    auto mo = JsonValue::object();
    mo.asObject()["interpolation"] = JsonValue(interpolation);
    mo.asObject()["requested_grid_cells"] = JsonValue(req.gridCells);
    mo.asObject()["effective_grid_cells"] = JsonValue(effectiveGrid);
    mo.asObject()["samples_per_cell"] = JsonValue(req.samplesPerCell);
    o["method"] = std::move(mo);

    // Real cryptographic integrity tag: SHA-256 over the canonical JSON of
    // every field above.
    std::string payload = root.dump();
    o["checksum"] = JsonValue(std::string("sha256:") + Sha256::hex(payload));
    return root;
}

} // namespace

HttpReply handleParameterize(const std::string& body) {
    try {
        JsonValue req = JsonValue::parse(body);
        if (!req.is(JsonValue::Type::Object))
            throw ApiError(ErrorCode::BAD_REQUEST, "request body must be an object");

        int dof;
        if (req.contains("joint_names") &&
            req.at("joint_names").is(JsonValue::Type::Array)) {
            dof = static_cast<int>(req.at("joint_names").asArray().size());
            if (dof <= 0)
                throw ApiError(ErrorCode::BAD_REQUEST,
                               "'joint_names' must be non-empty");
        } else if (req.contains("dof")) {
            if (!req.at("dof").is(JsonValue::Type::Number))
                throw ApiError(ErrorCode::BAD_REQUEST, "'dof' must be a number");
            dof = static_cast<int>(req.at("dof").asNumber());
            if (dof <= 0)
                throw ApiError(ErrorCode::BAD_REQUEST, "'dof' must be positive");
        } else {
            throw ApiError(ErrorCode::BAD_REQUEST,
                           "provide 'joint_names' (non-empty array) or 'dof'");
        }

        PlanningRequest pr;
        pr.waypoints = parsePoints(req.at("points"), dof);
        pr.vMax = parseLimitVector(req, "velocity_limits", dof);
        pr.aMax = parseLimitVector(req, "acceleration_limits", dof);

        if (req.contains("grid_cells"))
            pr.gridCells = static_cast<int>(req.at("grid_cells").asNumber());
        if (req.contains("samples_per_cell"))
            pr.samplesPerCell =
                static_cast<int>(req.at("samples_per_cell").asNumber());
        if (req.contains("duplicate_tolerance"))
            pr.duplicateTolerance =
                req.at("duplicate_tolerance").asNumber();
        if (req.contains("start_velocity")) {
            const auto& sv = req.at("start_velocity");
            if (!sv.is(JsonValue::Type::Array))
                throw ApiError(ErrorCode::BAD_REQUEST,
                               "'start_velocity' must be an array");
            Eigen::VectorXd v(dof);
            for (int j = 0; j < dof; ++j)
                v(j) = sv.asArray()[j].asNumber();
            pr.startVelocity = v;
        }

        // Run the real computation.
        Result res = plan(pr);

        const int segments = static_cast<int>(pr.waypoints.cols()) - 1;
        int effectiveGrid =
            std::max(1, pr.gridCells / std::max(1, segments)) *
            std::max(1, segments);
        JsonValue out = buildSuccess(
            res, pr,
            "natural cubic spline, C2, uniform knots (h=1) in path "
            "coordinate u; time-optimal path parameterization (TOPP) with "
            "pairwise velocity/acceleration MVC + backward/forward implicit "
            "Euler passes",
            effectiveGrid);
        return {200, "application/json", out.dump()};
    } catch (const ApiError& e) {
        return {statusFor(e.code), "application/json",
                errorBody(codeName(e.code), e.what()).dump()};
    } catch (const std::exception& e) {
        return {400, "application/json",
                errorBody("BAD_REQUEST",
                          std::string("invalid request: ") + e.what())
                    .dump()};
    }
}

HttpReply handleHealth() {
    auto o = JsonValue::object();
    o.asObject()["status"] = JsonValue("ok");
    o.asObject()["service"] = JsonValue("joint-trajectory-time-parameterization");
    std::string payload = o.dump();
    o.asObject()["checksum"] =
        JsonValue(std::string("sha256:") + Sha256::hex(payload));
    return {200, "application/json", o.dump()};
}

} // namespace topp
