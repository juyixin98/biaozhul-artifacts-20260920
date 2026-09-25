#include "ray_aabb.h"

namespace raybox {

bool intersectRayAABB(const Vec3& origin, const Vec3& dir, const AABB& box,
                      int32_t id, Hit& out) {
  double tEnter = -INFINITY;  // nearest of the slab entry distances
  double tExit = INFINITY;    // farthest of the slab exit distances
  int entryAxis = -1;         // axis whose near plane gave tEnter
  int entrySign = 0;          // +1: entered through the low face, -1 high face

  for (int axis = 0; axis < 3; ++axis) {
    double o = origin[axis];
    double d = dir[axis];
    double lo = box.min[axis];
    double hi = box.max[axis];
    double eps = axisTolerance(o, lo, hi);

    if (d == 0.0) {
      // Zero direction component: handled explicitly. The ray is parallel to
      // this slab; it can only meet the box if the origin lies within the
      // (possibly zero-thickness) slab, boundary included.
      if (o < lo - eps || o > hi + eps) return false;
      // Origin inside this slab: no constraint on the t interval, and a
      // zero-dir axis never provides the entry face normal.
      continue;
    }

    double t1 = (lo - o) / d;  // crossing of the low  plane
    double t2 = (hi - o) / d;  // crossing of the high plane
    double tNear = t1;
    double tFar = t2;
    // Outward normal of the low face points along -axis; the high face along
    // +axis. Record which face the ray enters through.
    int nearSign = -1;
    if (t1 > t2) {
      tNear = t2;
      tFar = t1;
      nearSign = +1;
    }

    // The narrowest interval [max of near, min of far] over all slabs is the
    // intersection interval.
    if (tNear > tEnter) {
      tEnter = tNear;
      entryAxis = axis;
      entrySign = nearSign;
    }
    if (tFar < tExit) tExit = tFar;
  }

  if (tEnter > tExit) return false;

  // t <= 0: the origin is inside the box (or exactly on the entry face).
  // Tolerance lets a point computed slightly "outside" still count as inside.
  double t0 = -EPS_ABS;
  double t;
  Vec3 normal(0.0, 0.0, 0.0);
  if (tEnter <= t0) {
    t = 0.0;
  } else {
    t = tEnter;
    if (entryAxis >= 0) normal[entryAxis] = static_cast<double>(entrySign);
  }

  // Negative-exit guard: an interval entirely behind the ray misses even when
  // finite-precision tEnter is a tiny positive (should not normally trigger
  // given the t0 rule above, kept as a belt-and-braces check).
  if (tExit < 0.0) return false;

  out.id = id;
  out.t = t;
  out.exit_t = tExit;
  out.point = origin + dir * t;
  out.normal = normal;
  return true;
}

}  // namespace raybox
