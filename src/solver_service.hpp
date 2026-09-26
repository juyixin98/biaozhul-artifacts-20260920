// solver_service.hpp - Translate JSON requests into solver calls and back.
#ifndef DIFFCON_SOLVER_SERVICE_HPP
#define DIFFCON_SOLVER_SERVICE_HPP

#include <string>

#include "json.hpp"

namespace diffcon {

// Process one solve request (JSON text). Always returns a JSON object:
// {"status": "ok", ...} for well-formed requests and
// {"status": "error", "error": {...}} for invalid ones. Never throws.
json::Value handleSolveRequest(const std::string& body);

}  // namespace diffcon

#endif  // DIFFCON_SOLVER_SERVICE_HPP
