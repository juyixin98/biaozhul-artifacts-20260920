#include "app.hpp"

#include "crypto.hpp"

#include <nlohmann/json.hpp>

#include <chrono>
#include <cmath>
#include <string>

namespace tf {

using json = nlohmann::json;

namespace {

int httpStatusFor(ErrorCode c) {
  switch (c) {
    case ErrorCode::kFrameNotFound: return 404;
    case ErrorCode::kMultiParentConflict:
    case ErrorCode::kCycleDetected:
    case ErrorCode::kDuplicateTimestamp: return 409;
    case ErrorCode::kDisconnected:
    case ErrorCode::kInvalidQuaternion:
    case ErrorCode::kNoSamples:
    case ErrorCode::kOutOfRange: return 422;
    case ErrorCode::kOk: return 200;
  }
  return 500;
}

HttpResponse jsonError(int status, const std::string& code,
                       const std::string& message) {
  HttpResponse r;
  r.status = status;
  r.body = json{{"error", {{"code", code}, {"message", message}}}}.dump();
  return r;
}

bool finiteVec(const json& j) {
  if (!j.is_array() || j.size() != 3) return false;
  for (const auto& c : j)
    if (!c.is_number() || !std::isfinite(c.get<double>())) return false;
  return true;
}

Vector3 vecFromJson(const json& j) {
  return Vector3(j[0].get<double>(), j[1].get<double>(),
                 j[2].get<double>());
}

// rotation may be {"w":..,"x":..,"y":..,"z":..} only.
bool parseRotation(const json& j, Quaternion& out, std::string& err) {
  if (!j.is_object() || !j.contains("w") || !j.contains("x") ||
      !j.contains("y") || !j.contains("z")) {
    err = "rotation must be an object with w,x,y,z";
    return false;
  }
  double w = j["w"].get<double>(), x = j["x"].get<double>(),
         y = j["y"].get<double>(), z = j["z"].get<double>();
  QuaternionCheck chk = validateQuaternion(w, x, y, z, &out);
  if (!chk.ok) {
    err = chk.error;
    return false;
  }
  return true;
}

bool parseTransform(const json& j, Transform& out, std::string& err) {
  if (!j.is_object() || !j.contains("translation") ||
      !j.contains("rotation")) {
    err = "transform must contain 'translation' and 'rotation'";
    return false;
  }
  if (!finiteVec(j["translation"])) {
    err = "translation must be [x,y,z] finite numbers";
    return false;
  }
  out.t = vecFromJson(j["translation"]);
  return parseRotation(j["rotation"], out.q, err);
}

json vec3Json(const Vector3& v) {
  return {v.x(), v.y(), v.z()};
}

json quatJson(const Quaternion& q) {
  return {{"w", q.w()}, {"x", q.x()}, {"y", q.y()}, {"z", q.z()}};
}

json matrixJson(const Matrix4& m) {
  json rows = json::array();
  for (int i = 0; i < 4; ++i) {
    json row = json::array();
    for (int j = 0; j < 4; ++j) row.push_back(m(i, j));
    rows.push_back(std::move(row));
  }
  return rows;
}

json usedJson(const UsedSample& u) {
  json j{
      {"edge_child", u.frame},
      {"type", u.is_static ? "static" : "dynamic"},
      {"interpolated", u.interpolated},
      {"queried_us", u.queried_us},
  };
  if (!u.is_static) {
    j["sample_a"] = u.sample_a;
    j["sample_b"] = u.sample_b;
    j["stamp_a_us"] = u.stamp_a_us;
    j["stamp_b_us"] = u.stamp_b_us;
    j["alpha"] = u.alpha;
    // Timed interpolation evaluates exactly at the queried time, so the
    // per-edge residual is 0; global async spread is reported at top level
    // in latest mode (time_error_us).
    j["time_error_us"] = 0;
  }
  return j;
}

int64_t nowSec() {
  return std::chrono::duration_cast<std::chrono::seconds>(
             std::chrono::system_clock::now().time_since_epoch())
      .count();
}

// Minimal query-string parser: key=value&...
std::map<std::string, std::string> parseQuery(const std::string& q) {
  std::map<std::string, std::string> out;
  size_t i = 0;
  while (i < q.size()) {
    size_t amp = q.find('&', i);
    std::string part =
        q.substr(i, amp == std::string::npos ? std::string::npos : amp - i);
    size_t eq = part.find('=');
    if (eq != std::string::npos)
      out[urlDecode(part.substr(0, eq))] = urlDecode(part.substr(eq + 1));
    else if (!part.empty())
      out[urlDecode(part)] = "";
    if (amp == std::string::npos) break;
    i = amp + 1;
  }
  return out;
}

}  // namespace

std::string App::signingPayload(const std::string& method,
                                const std::string& path,
                                const std::string& timestamp,
                                const std::string& raw_body) {
  return method + "\n" + path + "\n" + timestamp + "\n" + raw_body;
}

App::App(std::string hmac_key, int64_t timestamp_tolerance_sec)
    : hmac_key_(std::move(hmac_key)), ts_tol_sec_(timestamp_tolerance_sec) {}

HttpResponse App::auth(const HttpRequest& req) {
  if (hmac_key_.empty()) return HttpResponse{};  // auth disabled
  if (req.target == "/healthz") return HttpResponse{};

  std::string ts = req.header("x-tf-timestamp");
  std::string sig = req.header("x-tf-signature");
  if (ts.empty() || sig.empty())
    return jsonError(401, "UNAUTHENTICATED",
                     "missing X-TF-Timestamp or X-TF-Signature header");

  int64_t ts_val = 0;
  try {
    ts_val = std::stoll(ts);
  } catch (...) {
    return jsonError(401, "BAD_TIMESTAMP", "timestamp must be unix seconds");
  }
  int64_t now = nowSec();
  if (std::llabs(now - ts_val) > ts_tol_sec_)
    return jsonError(401, "STALE_TIMESTAMP",
                     "request timestamp outside " +
                         std::to_string(ts_tol_sec_) + "s tolerance");

  std::string expect =
      hmacSha256Hex(hmac_key_,
                    signingPayload(req.method, req.raw_target, ts, req.body));
  if (!constantTimeEquals(expect, sig))
    return jsonError(401, "BAD_SIGNATURE", "HMAC-SHA256 verification failed");
  return HttpResponse{};
}

HttpResponse App::dispatch(const HttpRequest& req) {
  // auth() returns status 200 on success (also when auth is disabled).
  HttpResponse a = auth(req);
  if (a.status != 200) return a;

  try {
    if (req.target == "/healthz") return handleHealth(req);
    if (req.target == "/v1/static" && req.method == "POST")
      return handleAddStatic(req);
    if (req.target == "/v1/samples" && req.method == "POST")
      return handleAddSamples(req);
    if (req.target == "/v1/query" && req.method == "GET")
      return handleQuery(req);
    if (req.target == "/v1/tree" && req.method == "GET") return handleTree(req);

    if (req.target == "/v1/static" || req.target == "/v1/samples" ||
        req.target == "/v1/query" || req.target == "/v1/tree")
      return jsonError(405, "METHOD_NOT_ALLOWED", "method not allowed");
    return jsonError(404, "NOT_FOUND", "unknown path " + req.target);
  } catch (const TfError& e) {
    return jsonError(httpStatusFor(e.code), errorCodeName(e.code), e.what());
  } catch (const json::exception& e) {
    return jsonError(400, "BAD_JSON", std::string("JSON error: ") + e.what());
  } catch (const std::exception& e) {
    return jsonError(500, "INTERNAL", e.what());
  }
}

HttpResponse App::handleHealth(const HttpRequest&) {
  HttpResponse r;
  r.body = json{{"status", "ok"}}.dump();
  return r;
}

HttpResponse App::handleAddStatic(const HttpRequest& req) {
  json j = json::parse(req.body);
  if (!j.is_object() || !j.contains("parent") || !j.contains("child") ||
      !j.contains("transform"))
    return jsonError(400, "BAD_REQUEST",
                     "need {parent, child, transform}");
  std::string parent = j["parent"].get<std::string>();
  std::string child = j["child"].get<std::string>();
  Transform x;
  std::string err;
  if (!parseTransform(j["transform"], x, err))
    return jsonError(422, errorCodeName(ErrorCode::kInvalidQuaternion), err);

  tree_.addStatic(parent, child, x);
  HttpResponse r;
  r.body = json{{"ok", true},
                {"edge", {{"parent", parent},
                          {"child", child},
                          {"type", "static"}}}}
               .dump();
  return r;
}

HttpResponse App::handleAddSamples(const HttpRequest& req) {
  json j = json::parse(req.body);
  if (!j.is_object() || !j.contains("parent") || !j.contains("child") ||
      !j.contains("samples") || !j["samples"].is_array() ||
      j["samples"].empty())
    return jsonError(400, "BAD_REQUEST",
                     "need {parent, child, samples:[...]}");

  std::string parent = j["parent"].get<std::string>();
  std::string child = j["child"].get<std::string>();

  std::vector<Sample> incoming;
  for (const auto& sj : j["samples"]) {
    if (!sj.is_object() || !sj.contains("stamp_us") ||
        !sj.contains("transform"))
      return jsonError(400, "BAD_REQUEST",
                       "each sample needs stamp_us and transform");
    Sample s;
    s.stamp_us = sj["stamp_us"].get<int64_t>();
    std::string err;
    if (!parseTransform(sj["transform"], s.xform, err))
      return jsonError(422, errorCodeName(ErrorCode::kInvalidQuaternion), err);
    incoming.push_back(std::move(s));
  }

  tree_.addDynamicSamples(parent, child, incoming);

  size_t after = 0;
  int64_t first_us = 0, last_us = 0;
  for (const auto& n : tree_.dump()) {
    if (n.frame == child) {
      after = n.sample_count;
      first_us = n.first_stamp_us;
      last_us = n.last_stamp_us;
    }
  }

  HttpResponse r;
  r.body = json{{"ok", true},
                {"edge", {{"parent", parent},
                          {"child", child},
                          {"type", "dynamic"},
                          {"added", incoming.size()},
                          {"total_samples", after},
                          {"window",
                           {{"first_us", first_us}, {"last_us", last_us}}}}}}
               .dump();
  return r;
}

HttpResponse App::handleQuery(const HttpRequest& req) {
  auto q = parseQuery(req.query);
  auto need = [&](const char* k) -> std::string {
    auto it = q.find(k);
    return it == q.end() ? std::string() : it->second;
  };
  std::string from = need("from");
  std::string to = need("to");
  if (from.empty() || to.empty())
    return jsonError(400, "BAD_REQUEST", "query needs from=<frame>&to=<frame>");

  std::string latest = need("latest");
  bool use_latest = latest == "1" || latest == "true";
  std::string tstr = need("time_us");

  QueryResult res;
  int64_t qtime = 0;
  if (use_latest) {
    res = tree_.queryLatest(from, to);
  } else {
    if (tstr.empty())
      return jsonError(400, "BAD_REQUEST",
                       "provide time_us=<int> or latest=1");
    try {
      qtime = std::stoll(tstr);
    } catch (...) {
      return jsonError(400, "BAD_REQUEST", "time_us must be an integer");
    }
    res = tree_.query(from, to, qtime);
  }

  json trace = json::array();
  for (const auto& u : res.trace) trace.push_back(usedJson(u));

  json body{
      {"from", res.from_frame},
      {"to", res.to_frame},
      {"mode", res.latest ? "latest" : "time"},
      {"query_time_us", res.latest ? json(nullptr) : json(qtime)},
      {"transform",
       {{"translation", vec3Json(res.xform.t)},
        {"quaternion", quatJson(res.xform.q.normalized())},
        {"matrix", matrixJson(res.xform.matrix())}}},
      {"path", res.path},
      {"used_samples", trace},
      {"time_error_us", res.time_error_us},
  };
  if (!res.latest) {
    bool has_dynamic = false;
    for (const auto& u : res.trace)
      if (!u.is_static) has_dynamic = true;
    if (has_dynamic) {
      body["sample_window"] = {
          {"min_us", res.min_sample_time_us},
          {"max_us", res.max_sample_time_us},
          {"bounded", true},
          {"extrapolation", "forbidden"}};
    } else {
      body["sample_window"] = nullptr;  // static-only path has no time window
    }
  } else {
    body["sample_window"] = nullptr;
  }

  HttpResponse r;
  r.body = body.dump();
  return r;
}

HttpResponse App::handleTree(const HttpRequest&) {
  json frames = json::array();
  for (const auto& n : tree_.dump()) {
    json fj{
        {"frame", n.frame},
        {"parent", n.parent.empty() ? json(nullptr) : json(n.parent)},
        {"type", n.parent.empty() ? "root"
                                  : (n.is_static ? "static" : "dynamic")},
        {"sample_count", n.sample_count},
    };
    if (!n.is_static && n.sample_count > 0)
      fj["window"] = {{"first_us", n.first_stamp_us},
                      {"last_us", n.last_stamp_us}};
    frames.push_back(std::move(fj));
  }
  HttpResponse r;
  r.body = json{{"frames", frames}, {"count", frames.size()}}.dump();
  return r;
}

}  // namespace tf
