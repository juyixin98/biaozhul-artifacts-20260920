// Independent validator: consumes a SERIALIZED solve response and checks the
// tree decomposition from scratch. It does not trust tree_decomposition.cpp
// and recomputes the elimination replay itself.
#pragma once

#include <string>
#include <vector>

#include "json.hpp"

namespace tdw {

struct ValidationReport {
    bool valid = false;
    std::vector<std::string> errors;
    // Evidence counters (0-initialized even when validation cannot run).
    int vertexCount = 0;
    int edgeCount = 0;
    int bagCount = 0;
    int bagTreeEdgeCount = 0;
    int uncoveredEdges = 0;
    int verticesMissingFromBags = 0;
    int runningIntersectionViolations = 0;
    int reportedWidth = -1;
    int recomputedWidth = -1;
    int maxBagSize = 0;
    bool replayWidthMatches = false;
    bool fillEdgesMatchReplay = false;
    bool widthLabeledNonOptimal = false;

    Json toJson() const;
};

// Validates a solve/exact response object: graph must be embedded, the
// elimination order must be a permutation, the bags/tree must satisfy vertex
// coverage, edge coverage, connectedness and the running-intersection
// property, and the reported width must match an independent replay.
ValidationReport validateResponse(const Json& response);

} // namespace tdw
