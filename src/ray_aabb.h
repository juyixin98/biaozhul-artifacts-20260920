// ray_aabb.h — ray / axis-aligned bounding box intersection (slab method).
//
// Semantics (full details in README.md):
//   * Boxes are CLOSED: a ray touching a face/edge/corner (grazing) is a hit.
//   * Degenerate boxes are supported: any extent may be zero (flat slab,
//     column, or a single point).
//   * A zero direction component is handled as its own case (parallel slab),
//     never by dividing by zero.
//   * A ray whose origin lies inside/on the box hits with t = 0; the reported
//     normal is then (0,0,0) ("no entry face").
//   * t values are measured along the ray P(t) = origin + t*dir; dir is used
//     as given (no normalization), so t is a distance only for unit dirs.
#pragma once

#include "vec3.h"

#include <cstdint>

namespace raybox {

struct AABB {
  Vec3 min;
  Vec3 max;
};

struct Hit {
  int32_t id = -1;      // user-provided box id
  double t = 0.0;       // entry parameter, P(t) = origin + t*dir
  double exit_t = 0.0;  // exit parameter (the far plane)
  Vec3 point;           // entry point coordinates
  Vec3 normal;          // entry face outward normal, or (0,0,0) if t == 0
};

// Numeric tolerances:
//   EPS_REL scales with the magnitudes involved so a 1e9-sized scene and a
//   1e-6-sized scene both get a sensible boundary slack. EPS_ABS guards the
//   near-zero scale.
constexpr double EPS_REL = 1e-12;
constexpr double EPS_ABS = 1e-15;

inline double axisTolerance(double o, double lo, double hi) {
  double span = hi - lo;
  double m = std::fabs(o);
  if (std::fabs(lo) > m) m = std::fabs(lo);
  if (std::fabs(hi) > m) m = std::fabs(hi);
  if (span > m) m = span;
  return EPS_ABS + EPS_REL * m;
}

// Intersect a single box. Returns true on a hit and fills `out`.
// Robust to: dir components equal to exactly 0, zero-extent boxes, origins
// on a boundary. Caller guarantees all inputs are finite.
bool intersectRayAABB(const Vec3& origin, const Vec3& dir, const AABB& box,
                      int32_t id, Hit& out);

}  // namespace raybox
