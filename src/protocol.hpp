#pragma once

#include <string>

#include "json.hpp"

namespace mcut {

struct RequestError {
  std::string code;
  std::string message;
};

// Processes one JSON request document and returns the JSON response
// document. Validation failures are reported via `error` (a normal error
// response object) rather than thrown, so the CLI can print them cleanly.
JsonValue handle_request(const JsonValue& request, RequestError& error);

// Builds a standardized {"status":"error", ...} response.
JsonValue error_response(const std::string& code, const std::string& message);

}  // namespace mcut
