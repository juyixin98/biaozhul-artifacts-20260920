#pragma once
// Ray traversal on a 2-D regular grid.
//
// Two independent implementations of exactly the same semantics are
// provided so the offline service can be checked beam-by-beam:
//
//   * traceBeamAmanatidesWoo() — exact integer grid walking
//     (Amanatides & Woo 1987), the production update;
//   * traceBeamSampled() — the "per-beam reference": parameterised dense
//     sampling along each ray, simple enough to audit by hand.
//
// Both emit, in traversal order, events of the form:
//   {cell index, event type}  with type 0 = free, 1 = occupied endpoint.
// Rules both obey:
//   * the sensor cell is marked free unless the ray length is 0;
//   * for a returned beam the final cell (endpoint) is occupied, and every
//     cell strictly before it along the ray is free;
//   * for a no-return beam there is no occupied event at all: every cell
//     visited up to travel (= max range) is free;
//   * traversal stops at the map boundary; cells outside the [0,w)x[0,h)
//     map are never emitted (a returned beam whose endpoint is outside is
//     "clipped": only the in-map prefix is updated, all free).

#include <cstdint>
#include <functional>
#include <vector>

namespace gridfusion {

struct RayEvent {
    int index;       // y * width + x
    int cx, cy;
    int occupied;    // 1 = hit endpoint, 0 = traversed free
};

using EventSink = std::function<void(const RayEvent&)>;

// Shared direction normalisation: both traversals must see exactly the same
// unit vector. Components smaller than this (relative to the other one) are
// numerical noise around an axis-aligned ray and are snapped to zero, so a
// bearing computed as pi/3 - pi/6 + pi/6 traces as an exact horizontal ray.
void normalizeDirection(double dx, double dy, double& ux, double& uy);

// Returns true when the ray hit (or, for a no-return beam, reached) the map
// boundary — i.e. the beam was clipped. The sensor (sx,sy) must be inside.
bool traceBeamAmanatidesWoo(double sx, double sy, double dx, double dy,
                            double travel, int width, int height,
                            double origin_x, double origin_y,
                            double resolution, bool no_return,
                            const EventSink& sink);

bool traceBeamSampled(double sx, double sy, double dx, double dy,
                      double travel, int width, int height,
                      double origin_x, double origin_y, double resolution,
                      bool no_return, const EventSink& sink);

}  // namespace gridfusion
