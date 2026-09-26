// SPDX-License-Identifier: MIT
// Request validation and action dispatch. Shared by the HTTP server and
// the stdin CLI so both speak exactly one JSON dialect.
#ifndef DAGPATHS_API_H
#define DAGPATHS_API_H

#include <string>

#include "json.h"

namespace dagpaths {

// Handles one request envelope:
//   {"action": "count"|"kth"|"rank"|"enumerate", ...action fields...}
// Never throws: all validation/algorithm failures become
//   {"ok": false, "error": {"code": ..., "message": ...}}
// and success becomes {"ok": true, "data": {...}}.
JsonValue handleRequest(const JsonValue& request);

JsonValue errorResponse(const std::string& code, const std::string& message);

} // namespace dagpaths

#endif
