// io_json.hpp — JSON <-> parameterization types (nlohmann/json based).
#pragma once

#include <nlohmann/json.hpp>
#include <string>

#include "trajectory_time_parameterization.hpp"

namespace jtp {

nlohmann::json VectorToJson(const Eigen::VectorXd& v);
Eigen::VectorXd JsonToVector(const nlohmann::json& j);

// Throws ParameterizeException (400/422) on malformed JSON.
ParameterizeRequest ParseRequestJson(const std::string& body);

nlohmann::json ResultToJson(const ParameterizeResult& result,
                            const std::string& request_sha256);

nlohmann::json ErrorToJson(const ApiError& error);

}  // namespace jtp
