#include "bvh.h"

#include <algorithm>
#include <cmath>
#include <limits>

namespace raybox {

namespace {

// Node-level slab test. Returns the ray/box overlap interval [tmin,tmax]
// (tmin may be negative when the origin is inside the node). Mirrors the
// zero-direction handling of intersectRayAABB.
bool slabInterval(const Vec3& origin, const Vec3& dir, const AABB& b,
                  double& tmin, double& tmax) {
  tmin = -INFINITY;
  tmax = INFINITY;
  for (int axis = 0; axis < 3; ++axis) {
    double o = origin[axis];
    double d = dir[axis];
    double lo = b.min[axis];
    double hi = b.max[axis];
    double eps = axisTolerance(o, lo, hi);

    if (d == 0.0) {
      if (o < lo - eps || o > hi + eps) return false;
      continue;
    }
    double t1 = (lo - o) / d;
    double t2 = (hi - o) / d;
    double tn = std::min(t1, t2);
    double tf = std::max(t1, t2);
    tmin = std::max(tmin, tn);
    tmax = std::min(tmax, tf);
    if (tmin > tmax) return false;
  }
  return true;
}

}  // namespace

// ---------------------------------------------------------------------------
// Construction
// ---------------------------------------------------------------------------

BVH::BVH(const std::vector<AABB>& boxes) : boxes_(&boxes) {
  if (boxes.empty()) return;
  std::vector<int32_t> ids(boxes.size());
  for (size_t i = 0; i < boxes.size(); ++i) ids[i] = static_cast<int32_t>(i);
  build(ids, 0, static_cast<int>(ids.size()));
}

void BVH::nodeBounds(const std::vector<int32_t>& ids, int begin, int end,
                     AABB& out) const {
  const double INF = std::numeric_limits<double>::infinity();
  out.min = Vec3(INF, INF, INF);
  out.max = Vec3(-INF, -INF, -INF);
  for (int i = begin; i < end; ++i) {
    const AABB& b = (*boxes_)[ids[i]];
    for (int axis = 0; axis < 3; ++axis) {
      out.min[axis] = std::min(out.min[axis], b.min[axis]);
      out.max[axis] = std::max(out.max[axis], b.max[axis]);
    }
  }
}

int32_t BVH::build(std::vector<int32_t>& ids, int begin, int end) {
  Node node;
  nodeBounds(ids, begin, end, node.bounds);

  if (end - begin == 1) {
    node.boxId = ids[begin];
    nodes_.push_back(node);
    return static_cast<int32_t>(nodes_.size() - 1);
  }

  // Longest axis of the combined bounds.
  Vec3 extent = node.bounds.max - node.bounds.min;
  int axis = 0;
  if (extent.y > extent[axis]) axis = 1;
  if (extent.z > extent[axis]) axis = 2;

  int mid = begin + (end - begin) / 2;
  std::nth_element(
      ids.begin() + begin, ids.begin() + mid, ids.begin() + end,
      [&](int32_t a, int32_t b) {
        double ca = 0.5 * ((*boxes_)[a].min[axis] + (*boxes_)[a].max[axis]);
        double cb = 0.5 * ((*boxes_)[b].min[axis] + (*boxes_)[b].max[axis]);
        return ca < cb;
      });

  // Reserve the parent slot first so indices returned by the recursive
  // builds stay valid; fill children afterwards.
  int32_t parentIdx = static_cast<int32_t>(nodes_.size());
  nodes_.push_back(node);
  node.left = build(ids, begin, mid);
  node.right = build(ids, mid, end);
  nodes_[parentIdx] = node;
  return parentIdx;
}

// ---------------------------------------------------------------------------
// Queries
// ---------------------------------------------------------------------------

bool BVH::nearest(const Vec3& origin, const Vec3& dir, Hit& out) const {
  if (nodes_.empty()) return false;

  int32_t stack[64];
  int sp = 0;
  stack[sp++] = 0;

  double bestT = INFINITY;
  int32_t bestId = -1;
  Hit leafHit;

  while (sp > 0) {
    int32_t ni = stack[--sp];
    const Node& n = nodes_[ni];

    if (n.leaf()) {
      const AABB& b = (*boxes_)[n.boxId];
      if (intersectRayAABB(origin, dir, b, n.boxId, leafHit)) {
        // Deterministic tie-break on exactly equal t: smallest box id wins,
        // matching brute-force iteration order.
        if (leafHit.t < bestT ||
            (leafHit.t == bestT && n.boxId < bestId)) {
          bestT = leafHit.t;
          bestId = n.boxId;
          out = leafHit;
        }
      }
      continue;
    }

    double lMin, lMax, rMin, rMax;
    bool hitL = slabInterval(origin, dir, nodes_[n.left].bounds, lMin, lMax);
    bool hitR = slabInterval(origin, dir, nodes_[n.right].bounds, rMin, rMax);
    if (hitL) hitL = (lMax >= 0.0 && lMin <= bestT);
    if (hitR) hitR = (rMax >= 0.0 && rMin <= bestT);

    // Push the nearer child last so it is popped first; prune a child whose
    // whole interval starts beyond the current best hit.
    if (hitL && hitR) {
      if (lMin <= rMin) {
        stack[sp++] = n.right;
        stack[sp++] = n.left;
      } else {
        stack[sp++] = n.left;
        stack[sp++] = n.right;
      }
    } else if (hitL) {
      stack[sp++] = n.left;
    } else if (hitR) {
      stack[sp++] = n.right;
    }
  }

  return bestId >= 0;
}

std::vector<Hit> BVH::allHits(const Vec3& origin, const Vec3& dir) const {
  std::vector<Hit> hits;
  if (nodes_.empty()) return hits;

  int32_t stack[64];
  int sp = 0;
  stack[sp++] = 0;
  Hit leafHit;

  while (sp > 0) {
    int32_t ni = stack[--sp];
    const Node& n = nodes_[ni];
    if (n.leaf()) {
      const AABB& b = (*boxes_)[n.boxId];
      if (intersectRayAABB(origin, dir, b, n.boxId, leafHit))
        hits.push_back(leafHit);
      continue;
    }
    double tmin, tmax;
    if (slabInterval(origin, dir, nodes_[n.left].bounds, tmin, tmax) &&
        tmax >= 0.0)
      stack[sp++] = n.left;
    if (slabInterval(origin, dir, nodes_[n.right].bounds, tmin, tmax) &&
        tmax >= 0.0)
      stack[sp++] = n.right;
  }

  sortHits(hits);
  return hits;
}

// ---------------------------------------------------------------------------
// Brute-force reference and shared sorting
// ---------------------------------------------------------------------------

bool bruteForceNearest(const std::vector<AABB>& boxes, const Vec3& origin,
                       const Vec3& dir, Hit& out) {
  bool any = false;
  Hit h;
  for (size_t i = 0; i < boxes.size(); ++i) {
    if (intersectRayAABB(origin, dir, boxes[i], static_cast<int32_t>(i), h) &&
        (!any || h.t < out.t)) {
      out = h;
      any = true;
    }
  }
  return any;
}

std::vector<Hit> bruteForceAll(const std::vector<AABB>& boxes,
                               const Vec3& origin, const Vec3& dir) {
  std::vector<Hit> hits;
  Hit h;
  for (size_t i = 0; i < boxes.size(); ++i) {
    if (intersectRayAABB(origin, dir, boxes[i], static_cast<int32_t>(i), h))
      hits.push_back(h);
  }
  sortHits(hits);
  return hits;
}

void sortHits(std::vector<Hit>& hits) {
  // Defensive: a NaN t would make the comparator neither a strict weak order
  // nor finite, which is undefined behavior for std::sort. Validated input
  // cannot produce one; dropping it guarantees the guarantee regardless.
  hits.erase(std::remove_if(hits.begin(), hits.end(),
                            [](const Hit& h) {
                              return !std::isfinite(h.t) ||
                                     !std::isfinite(h.exit_t);
                            }),
             hits.end());
  std::stable_sort(hits.begin(), hits.end(), [](const Hit& a, const Hit& b) {
    if (a.t != b.t) return a.t < b.t;
    return a.id < b.id;  // documented deterministic tie-break
  });
}

}  // namespace raybox
