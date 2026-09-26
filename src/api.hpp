// JSON request/response layer: validates the request, maps original vertex
// IDs to indices, runs the matching core, verifies the certificate, and
// builds the JSON response with original IDs preserved.
#pragma once

#include "json.hpp"

// Hard limits to keep the service bounded (documented in README).
constexpr long long kMaxVerticesPerSide = 20000;
constexpr long long kMaxEdges = 200000;

// Solves one request. On validation failure returns an object
// {"ok": false, "error": "..."} instead of throwing.
JsonValue solveRequest(const JsonValue& request);
