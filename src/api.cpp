#include "api.h"

#include "icp.h"
#include "json.h"
#include "sha256.h"

#include <cmath>

namespace pcr {

namespace {

Json errorBody(const std::string& code, const std::string& message) {
    Json e = Json::makeObject();
    e.set("code", Json::makeString(code));
    e.set("message", Json::makeString(message));
    Json b = Json::makeObject();
    b.set("error", std::move(e));
    return b;
}

HttpResponse jsonResponse(int status, Json body, const std::string& contentHash = "") {
    HttpResponse r;
    r.status = status;
    r.body = body.dump();
    if (!contentHash.empty())
        r.headers.emplace_back("X-Content-SHA256", contentHash);
    return r;
}

bool extractPoint(const Json& row, Eigen::Vector3d& out) {
    if (!row.isArray()) return false;
    const auto& a = row.asArray();
    if (a.size() != 3) return false;
    for (const auto& v : a)
        if (!v.isNumber()) return false;
    out = Eigen::Vector3d(a[0].asNumber(), a[1].asNumber(), a[2].asNumber());
    return true;
}

// Accepts either [[x,y,z],...] or {"points":[[x,y,z],...]}.
const Json* cloudNode(const Json& root, const std::string& key) {
    if (root.at(key).isObject() && root.at(key).contains("points"))
        return &root.at(key).at("points");
    return &root.at(key);
}

}  // namespace

HttpResponse handleRegister(const HttpRequest& req) {
    if (req.method != "POST")
        return jsonResponse(405, errorBody("method_not_allowed", "use POST"));

    // Integrity: when a hash header is present it MUST match the exact bytes received.
    if (req.hasContentHash()) {
        std::string actual = Sha256::hex(Sha256::hash(req.body));
        if (actual != req.contentSha256) {
            return jsonResponse(422, errorBody("content_hash_mismatch",
                "X-Content-SHA256 does not match SHA-256 of received body"));
        }
    }

    Json root;
    try {
        root = Json::parse(req.body);
    } catch (const std::exception& e) {
        return jsonResponse(400, errorBody("invalid_json", e.what()));
    }
    if (!root.isObject())
        return jsonResponse(400, errorBody("invalid_request", "request body must be a JSON object"));
    if (!root.contains("source") || !root.contains("target"))
        return jsonResponse(400, errorBody("missing_cloud", "fields 'source' and 'target' are required"));

    const Json* sn;
    const Json* tn;
    try {
        sn = cloudNode(root, "source");
        tn = cloudNode(root, "target");
    } catch (const std::exception& e) {
        return jsonResponse(400, errorBody("invalid_cloud", e.what()));
    }
    if (!sn->isArray() || !tn->isArray())
        return jsonResponse(400, errorBody("invalid_cloud", "'source' and 'target' must be arrays"));
    if (sn->asArray().size() > kMaxPoints || tn->asArray().size() > kMaxPoints)
        return jsonResponse(400, errorBody("too_many_points", "each cloud is limited to 5000 points"));

    std::vector<Eigen::Vector3d> srcRaw, tgtRaw;
    srcRaw.reserve(sn->asArray().size());
    tgtRaw.reserve(tn->asArray().size());
    for (const auto& row : sn->asArray()) {
        Eigen::Vector3d p;
        if (!extractPoint(row, p))
            return jsonResponse(400, errorBody("invalid_point", "each point must be [x,y,z] of numbers"));
        srcRaw.push_back(p);
    }
    for (const auto& row : tn->asArray()) {
        Eigen::Vector3d p;
        if (!extractPoint(row, p))
            return jsonResponse(400, errorBody("invalid_point", "each point must be [x,y,z] of numbers"));
        tgtRaw.push_back(p);
    }

    PointCloud3 source = cleanCloud(srcRaw);
    PointCloud3 target = cleanCloud(tgtRaw);

    IcpOptions opts;
    if (root.contains("max_correspondence_distance")) {
        const Json& v = root.at("max_correspondence_distance");
        if (!v.isNumber() || !std::isfinite(v.asNumber()) || v.asNumber() <= 0)
            return jsonResponse(400, errorBody("invalid_parameter",
                "'max_correspondence_distance' must be a positive finite number"));
        opts.maxCorrespondenceDistance = v.asNumber();
    }
    if (root.contains("max_iterations")) {
        const Json& v = root.at("max_iterations");
        if (!v.isNumber() || v.asNumber() < 1 || v.asNumber() != std::floor(v.asNumber()))
            return jsonResponse(400, errorBody("invalid_parameter",
                "'max_iterations' must be a positive integer (<=200 used)"));
        opts.maxIterations = static_cast<size_t>(v.asNumber());
    }

    Eigen::Matrix4d init = Eigen::Matrix4d::Identity();
    if (root.contains("initial_pose")) {
        const Json& m = root.at("initial_pose");
        if (!m.isArray() || m.asArray().size() != 4)
            return jsonResponse(400, errorBody("invalid_pose", "'initial_pose' must be a 4x4 matrix"));
        for (int i = 0; i < 4; ++i) {
            if (!m.asArray()[i].isArray() || m.asArray()[i].asArray().size() != 4)
                return jsonResponse(400, errorBody("invalid_pose", "'initial_pose' must be a 4x4 matrix"));
            for (int j = 0; j < 4; ++j) {
                const Json& e = m.asArray()[i].asArray()[j];
                if (!e.isNumber() || !std::isfinite(e.asNumber()))
                    return jsonResponse(400, errorBody("invalid_pose", "pose entries must be finite numbers"));
                init(i, j) = e.asNumber();
            }
        }
    }

    IcpResult r = runIcp(source, target, opts, root.contains("initial_pose") ? &init : nullptr);

    auto mat3 = [](const Eigen::Matrix3d& m) {
        Json a = Json::makeArray();
        for (int i = 0; i < 3; ++i) {
            Json row = Json::makeArray();
            for (int j = 0; j < 3; ++j) row.push(Json::makeNumber(m(i, j)));
            a.push(std::move(row));
        }
        return a;
    };
    auto vec3 = [](const Eigen::Vector3d& v) {
        Json a = Json::makeArray();
        for (int i = 0; i < 3; ++i) a.push(Json::makeNumber(v(i)));
        return a;
    };

    // Orthogonality / handedness of the rotation actually returned.
    double orthoErr = (r.rotation * r.rotation.transpose() - Eigen::Matrix3d::Identity()).norm();
    double det = r.rotation.determinant();

    Json out = Json::makeObject();
    out.set("ok", Json::makeBool(r.ok));
    out.set("converged", Json::makeBool(r.converged));
    out.set("high_confidence", Json::makeBool(r.highConfidence));
    out.set("degenerate", Json::makeBool(r.degenerate));
    out.set("convergence_reason", Json::makeString(reasonName(r.reason)));
    out.set("rotation", mat3(r.rotation));
    out.set("translation", vec3(r.translation));
    if (std::isfinite(r.rmse))
        out.set("residual_rmse", Json::makeNumber(r.rmse));
    else
        out.set("residual_rmse", Json());  // null when no correspondences exist
    out.set("inlier_ratio", Json::makeNumber(r.inlierRatio));
    out.set("correspondences", Json::makeNumber(static_cast<double>(r.correspondences)));
    out.set("iterations", Json::makeNumber(static_cast<double>(r.iterations)));
    out.set("rank_condition", Json::makeNumber(r.rankCondition));
    out.set("scale", Json::makeNumber(r.scale));
    out.set("orthogonality_error", Json::makeNumber(orthoErr));
    out.set("rotation_det", Json::makeNumber(det));
    Json stats = Json::makeObject();
    stats.set("source_finite", Json::makeNumber(static_cast<double>(source.points.cols())));
    stats.set("target_finite", Json::makeNumber(static_cast<double>(target.points.cols())));
    stats.set("source_dropped_nonfinite", Json::makeNumber(static_cast<double>(source.droppedNonFinite)));
    stats.set("target_dropped_nonfinite", Json::makeNumber(static_cast<double>(target.droppedNonFinite)));
    out.set("points", std::move(stats));
    if (!r.error.empty()) out.set("detail", Json::makeString(r.error));

    // The response is hashed too: clients can verify they received exact bytes.
    std::string serialized = out.dump();
    std::string hash = Sha256::hex(Sha256::hash(serialized));
    HttpResponse hr;
    hr.status = 200;
    hr.body = std::move(serialized);
    hr.headers.emplace_back("X-Content-SHA256", hash);
    return hr;
}

HttpResponse handleVerifyHash(const HttpRequest& req) {
    if (req.method != "POST")
        return jsonResponse(405, errorBody("method_not_allowed", "use POST"));
    Json out = Json::makeObject();
    out.set("sha256", Json::makeString(Sha256::hex(Sha256::hash(req.body))));
    out.set("length", Json::makeNumber(static_cast<double>(req.body.size())));
    if (req.hasContentHash())
        out.set("claimed", Json::makeString(req.contentSha256));
    return jsonResponse(200, std::move(out));
}

HttpResponse handleHealth(const HttpRequest& req) {
    if (req.method != "GET")
        return jsonResponse(405, errorBody("method_not_allowed", "use GET"));
    Json out = Json::makeObject();
    out.set("status", Json::makeString("ok"));
    out.set("service", Json::makeString("point-cloud-registration"));
    out.set("max_points", Json::makeNumber(static_cast<double>(kMaxPoints)));
    return jsonResponse(200, std::move(out));
}

}  // namespace pcr
