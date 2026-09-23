// Triangular mesh topology validation — core checks (pure computation, no I/O).
//
// Coordinate system convention (see README):
//   - Input coordinates are treated as a right-handed Cartesian system in
//     metres. No datum / CRS transform is performed; the checker only consumes
//     dimensionless numeric triples, so any linear unit works as long as it is
//     consistent (eps is expressed in that same unit).
//   - Winding/orientation follows the right-hand rule (normal = (p1-p0)x(p2-p0)).
#pragma once

#include <array>
#include <cstdint>
#include <string>
#include <vector>

#include "json.hpp"

namespace meshcheck {

using Vec3 = std::array<double, 3>;

struct Options {
    // Absolute tolerance (input length unit). Degeneracy and vertex-merge
    // thresholds combine this with a relative (bounding-box) term.
    double epsAbs = 1e-9;
    // Merge distinct vertex indices whose coordinates coincide within
    // max(epsAbs, epsRel * bboxDiag). 0.0 disables duplicate-coordinate merge.
    double epsRel = 1e-9;
};

// Face after preprocessing; faces rejected at input time (bad indices,
// wrong size, NaN) are not represented here.
struct Face {
    int64_t id = -1;                    // user-facing id (input index or explicit)
    std::array<int64_t, 3> verts{{-1, -1, -1}}; // canonical (merged) vertex ids
    std::array<int64_t, 3> origVerts{{-1, -1, -1}}; // indices as given in the request
    size_t inputIndex = 0;              // position among all input faces
    bool degenerate = false;            // zero-area triangle (geometric)
    double area = 0.0;
};

struct DuplicateFaceGroup {
    std::vector<int64_t> faceIds;
};

struct EdgeInfo {
    int64_t a = 0;
    int64_t b = 0;             // canonical undirected edge, a < b
    std::vector<int64_t> faceIds;
    int64_t comp = -1;         // connected component assigned during analysis
};

struct Component {
    int64_t id = 0;
    int64_t faceCount = 0;     // valid (non-degenerate, non-duplicate) faces
    int64_t vertexCount = 0;   // vertices incident to those faces
    int64_t boundaryEdges = 0; // edges incident to exactly 1 face
    int64_t nonManifoldEdges = 0; // edges incident to 3+ faces
    bool closed = false;       // every edge incident to exactly 2 faces
};

struct Report {
    // Request-level structural problems
    std::vector<std::string> requestErrors;

    // Geometric degeneracy (kept separate from topology errors)
    std::vector<int64_t> degenerateFaces;
    std::vector<std::pair<int64_t, int64_t>> duplicateVertices; // (keptId, mergedId)

    // Topology errors
    std::vector<int64_t> duplicateFaceIds;      // redundant copies (first copy kept)
    std::vector<EdgeInfo> nonManifoldEdges;     // 3+ incident faces
    std::vector<EdgeInfo> orientationConflicts; // 2 faces, inconsistent winding
    std::vector<EdgeInfo> boundaryEdges;        // 1 incident face (open holes)
    std::vector<int64_t> isolatedVertices;

    std::vector<DuplicateFaceGroup> duplicateFaceGroups;
    std::vector<Component> components;

    // Summary
    int64_t vertexCountInput = 0;
    int64_t faceCountInput = 0;
    int64_t faceCountAccepted = 0;  // in-range, 3 indices, finite coords
    int64_t faceCountTriangulated = 0; // used in topology analysis
    bool manifold = false;          // orientable 2-manifold: no non-manifold,
                                    // no orientation conflict, no duplicates
    bool orientable = false;
    bool closed = false;            // single closed component and no other errors
};

// Runs every check and returns the structured report.
Report runChecks(const json::Value& request, const Options& opts);

// Renders the report as a JSON response object.
json::Value buildResponse(const Report& r, const Options& opts);

} // namespace meshcheck
