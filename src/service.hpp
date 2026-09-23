// Request handling: parse polygon/point JSON with exact decimal scaling,
// validate, build indices, answer batched point-location queries.
#pragma once

#include <memory>
#include <string>
#include <vector>

#include "geometry.hpp"
#include "index.hpp"
#include "json.hpp"

struct PreparedPolygon {
    std::string id;
    long scale = 0;                     // common factor exponent S
    Polygon polygon;
    PolygonIndex index;
    // Bookkeeping for the response.
    std::vector<int> outerInputOrientation; // +1 CCW, -1 CW (per closed ring)
    std::vector<int> holeInputOrientation;
};

struct Service {
    std::vector<std::unique_ptr<PreparedPolygon>> polygons;

    // Process one request JSON string; returns response JSON string.
    std::string handle(const std::string& requestText, bool pretty);

    // Same but with parsed root (used by tests).
    JsonPtr handleNode(const JsonValue& root, std::string* errorOut);
};
