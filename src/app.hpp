#pragma once
//
// app.hpp — request validation and metric orchestration for the rectangle
// union backend. Pure functions over JSON text; shared by the CLI and the
// embedded HTTP entry point.
//
#include <string>
#include <vector>

#include "json.hpp"
#include "sweep.hpp"

namespace ru::app {

inline constexpr long long COORD_LIMIT = 1'000'000'000LL;

struct Response {
    int status;          // HTTP-ish status code, also used as CLI exit driver
    std::string body;    // JSON document
    bool ok() const { return status == 200; }
};

// Process a JSON request document:
//   { "rectangles": [ {"x1":..,"y1":..,"x2":..,"y2":.. ,"id": <optional string>}, ... ] }
// Extra/unknown fields are ignored.
Response processRequest(const std::string& payload);

// Render a signed 128-bit integer in decimal.
std::string i128ToString(ru::i128 v);

}  // namespace ru::app
