#include "io_json.hpp"

#include <Eigen/Dense>

#include <stdexcept>
#include <string>
#include <vector>

namespace jtp {

using nlohmann::json;

json VectorToJson(const Eigen::VectorXd& v) {
  json arr = json::array();
  for (int i = 0; i < v.size(); ++i) arr.push_back(v[i]);
  return arr;
}

Eigen::VectorXd JsonToVector(const json& j) {
  if (!j.is_array()) {
    throw ParameterizeException(
        ApiError{422, "INVALID_JSON", "expected an array of numbers"});
  }
  Eigen::VectorXd v(static_cast<int>(j.size()));
  for (size_t i = 0; i < j.size(); ++i) {
    if (!j[i].is_number()) {
      throw ParameterizeException(
          ApiError{422, "INVALID_JSON", "expected an array of numbers"});
    }
    v[static_cast<int>(i)] = j[i].get<double>();
  }
  return v;
}

static std::vector<Eigen::VectorXd> ParseWaypoints(const json& root) {
  if (!root.contains("waypoints") || !root["waypoints"].is_array() ||
      root["waypoints"].empty()) {
    throw ParameterizeException(
        ApiError{422, "INVALID_WAYPOINTS",
                 "field 'waypoints' must be a non-empty array of number arrays"});
  }
  std::vector<Eigen::VectorXd> out;
  for (const auto& w : root["waypoints"]) {
    if (!w.is_array() || w.empty()) {
      throw ParameterizeException(ApiError{
          422, "INVALID_WAYPOINTS", "every waypoint must be a non-empty array"});
    }
    out.push_back(JsonToVector(w));
  }
  return out;
}

ParameterizeRequest ParseRequestJson(const std::string& body) {
  json root;
  try {
    root = json::parse(body);
  } catch (const json::parse_error& e) {
    throw ParameterizeException(
        ApiError{400, "INVALID_JSON", std::string("malformed JSON: ") + e.what()});
  }
  if (!root.is_object()) {
    throw ParameterizeException(
        ApiError{422, "INVALID_JSON", "request body must be a JSON object"});
  }

  ParameterizeRequest req;
  req.waypoints = ParseWaypoints(root);

  const int dof = static_cast<int>(req.waypoints[0].size());
  if (!root.contains("velocity_limits") || !root.contains("acceleration_limits")) {
    throw ParameterizeException(ApiError{
        422, "INVALID_LIMITS",
        "fields 'velocity_limits' and 'acceleration_limits' are required"});
  }
  req.velocity_limits = JsonToVector(root["velocity_limits"]);
  req.acceleration_limits = JsonToVector(root["acceleration_limits"]);
  if (req.velocity_limits.size() != dof ||
      req.acceleration_limits.size() != dof) {
    throw ParameterizeException(
        ApiError{422, "INVALID_LIMITS",
                 "limit vector length must match the waypoint dimension (" +
                     std::to_string(dof) + ")"});
  }
  if (root.contains("start_velocity") && !root["start_velocity"].is_null()) {
    req.start_velocity = JsonToVector(root["start_velocity"]);
  }
  if (root.contains("end_velocity") && !root["end_velocity"].is_null()) {
    req.end_velocity = JsonToVector(root["end_velocity"]);
  }
  if (root.contains("dwell_time")) {
    if (!root["dwell_time"].is_number()) {
      throw ParameterizeException(
          ApiError{422, "INVALID_PARAMETERS", "dwell_time must be a number"});
    }
    req.dwell_time = root["dwell_time"].get<double>();
  }
  if (root.contains("samples_per_segment")) {
    if (!root["samples_per_segment"].is_number_integer() ||
        root["samples_per_segment"].get<int>() < 2) {
      throw ParameterizeException(ApiError{
          422, "INVALID_PARAMETERS", "samples_per_segment must be an integer >= 2"});
    }
    req.samples_per_segment = root["samples_per_segment"].get<int>();
  }
  return req;
}

json ResultToJson(const ParameterizeResult& r, const std::string& request_sha256) {
  json waypoints = json::array();
  for (const auto& s : r.states) {
    waypoints.push_back({
        {"time", s.time},
        {"position", VectorToJson(s.position)},
        {"velocity", VectorToJson(s.velocity)},
        {"acceleration", VectorToJson(s.acceleration)},
    });
  }

  json segments = json::array();
  for (const auto& s : r.segments) {
    segments.push_back({
        {"index", s.index},
        {"zero_length", s.zero_length},
        {"length", s.length},
        {"duration", s.duration},
        {"path_speed_ceiling", s.v_path_max},
        {"path_acceleration_ceiling", s.a_path_max},
        {"velocity_bottleneck_joint", s.velocity_bottleneck_joint},
        {"acceleration_bottleneck_joint", s.acceleration_bottleneck_joint},
        {"binding_regime", s.binding_regime},
        {"peak_path_speed", s.peak_path_speed},
        {"bottleneck_velocity_ratio", s.bottleneck_velocity_ratio},
        {"bottleneck_accel_ratio", s.bottleneck_accel_ratio},
    });
  }

  return {
      {"ok", true},
      {"dof", r.dof},
      {"interpolation", r.interpolation},
      {"request_sha256", request_sha256},
      {"zero_length_segments", r.zero_length_segments},
      {"waypoints", waypoints},
      {"segments", segments},
      {"verification",
       {
           {"passed", r.verification.passed},
           {"samples_per_segment", r.verification.samples_per_segment},
           {"total_samples", r.verification.total_samples},
           {"velocity_tolerance", r.verification.velocity_tolerance},
           {"acceleration_tolerance", r.verification.acceleration_tolerance},
           {"max_velocity_violation", r.verification.max_velocity_violation},
           {"max_acceleration_violation",
            r.verification.max_acceleration_violation},
           {"worst_velocity_joint", r.verification.worst_velocity_joint},
           {"worst_acceleration_joint", r.verification.worst_acceleration_joint},
           {"time_min_gap", r.verification.time_min_gap},
       }},
  };
}

json ErrorToJson(const ApiError& e) {
  return {{"ok", false}, {"error", {{"code", e.code}, {"message", e.message}}}};
}

}  // namespace jtp
