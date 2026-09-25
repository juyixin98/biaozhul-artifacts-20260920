// api.h — request validation and execution for the JSON entry point.
//
// Request (see examples/ for full samples):
// {
//   "ray":     { "origin": [x,y,z], "dir": [dx,dy,dz] },
//   "boxes":   [ {"id": int, "min":[...], "max":[...]}, ... ],
//   "mode":    "nearest" | "all",        // optional, default "nearest"
//   "use_bvh": true | false              // optional, default true
// }
//
// All coordinates must be finite JSON numbers. The direction must be
// non-zero. Boxes must satisfy min <= max componentwise. Violations return
// {"ok":false,"error":{...}} with HTTP-style codes (400) embedded in JSON.
#pragma once

#include <string>

namespace raybox {

// Parses, validates, executes and serializes one request. Never throws:
// malformed input yields a JSON error document.
std::string handleRequest(const std::string& requestJson);

}  // namespace raybox
