// Exact integer geometry core for point-in-polygon.
//
// Coordinate system: abstract 2D Cartesian plane (NOT geographic lon/lat).
// Every coordinate is a cpp_int obtained by scaling finite decimal inputs by
// one common factor 10^S for the whole request, so all predicates are exact
// integer arithmetic. There is no epsilon: a point is "on the boundary" only
// when it lies on an edge exactly.
#pragma once

#include <optional>
#include <string>
#include <vector>

#include "decimal.hpp"

struct Pt {
    Int x = 0;
    Int y = 0;
    bool operator==(const Pt& o) const { return x == o.x && y == o.y; }
    bool operator!=(const Pt& o) const { return !(*this == o); }
};

struct Ring {
    std::vector<Pt> v; // closed representation: v.back() == v.front()
    bool hole = false;
};

struct Polygon {
    Ring outer;
    std::vector<Ring> holes;
};

// ---- Exact predicates --------------------------------------------------

// Sign of cross(b-a, c-a): >0 CCW, <0 CW, 0 collinear.
Int cross(const Pt& a, const Pt& b, const Pt& c);

// Twice the signed area of a (closed) ring. >0 CCW in Cartesian coordinates.
Int ringSignedArea2(const Ring& r);

// Is p on segment ab exactly (boundary test)?
bool onSegment(const Pt& p, const Pt& a, const Pt& b);

// Proper or improper intersection of closed segments ab and cd, sharing the
// known common vertex at index `shared` (0 => a==c, 1 => a==d,
// 2 => b==c, 3 => b==d), or -1 for no known shared vertex.
// Collinear overlap is always an intersection.
bool segmentsIntersectShared(const Pt& a, const Pt& b, const Pt& c, const Pt& d,
                             int shared);

// ---- Validation --------------------------------------------------------

struct ValidationResult {
    bool ok = false;
    std::string errorCode;    // e.g. "SELF_INTERSECTING_RING"
    std::string message;
};

// Validate ring geometry. Input may be open (first != last); on success the
// ring is normalized to closed form, consecutive duplicate points removed,
// and rotated to start at a deterministic vertex.
ValidationResult validateAndNormalizeRing(Ring& r);

// Validate outer + holes. Requires every ring already normalized.
// On success canonicalizes orientation: outer CCW (area2 > 0), holes CW.
ValidationResult validatePolygon(Polygon& poly);

// ---- Location classification ------------------------------------------

enum class Rel { Inside, Outside, Bound };

const char* relName(Rel r);

// Naive exact point-in-ring: boundary first, then horizontal-ray parity with
// the half-open edge rule [ymin, ymax). Handles vertices exactly.
Rel locateNaiveRing(const Ring& r, const Pt& p);

// Naive exact point-in-polygon-with-holes.
Rel locateNaivePolygon(const Polygon& poly, const Pt& p);
