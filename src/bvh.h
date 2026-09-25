// bvh.h — bounding volume hierarchy over a set of AABBs, with ray queries.
//
// Construction: median split along the longest axis of the node's bounding
// box (O(n log^2 n)); adequate for an offline backend and keeps the code
// dependency-free. Each leaf stores one box id.
//
// Traversal is an explicit stack (no recursion depth risk). Median-split
// depth is ceil(log2(n)) (<= 31 for int-indexed inputs), so a 64-entry stack
// cannot overflow any realistic input.
//   * nearest: track the best t and skip any node whose slab tmin > best;
//   * all:     visit every overlapping node and collect every leaf hit.
//
// Both query paths call the exact same intersectRayAABB primitive as the
// brute-force reference, so results match by construction.
#pragma once

#include "ray_aabb.h"
#include "vec3.h"

#include <cstdint>
#include <vector>

namespace raybox {

class BVH {
 public:
  BVH() = default;
  explicit BVH(const std::vector<AABB>& boxes);

  // Nearest hit only. Returns false when nothing is hit.
  bool nearest(const Vec3& origin, const Vec3& dir, Hit& out) const;

  // Every hit, sorted by ascending entry t. Ties (equal t up to the sort
  // epsilon) keep build/box order, which is stable and documented.
  std::vector<Hit> allHits(const Vec3& origin, const Vec3& dir) const;

  size_t nodeCount() const { return nodes_.size(); }

 private:
  struct Node {
    AABB bounds{};
    int32_t left = -1;   // child node index; -1 when leaf
    int32_t right = -1;
    int32_t boxId = -1;  // valid only when leaf
    bool leaf() const { return left < 0; }
  };

  int32_t build(std::vector<int32_t>& ids, int begin, int end);
  void nodeBounds(const std::vector<int32_t>& ids, int begin, int end,
                  AABB& out) const;

  const std::vector<AABB>* boxes_ = nullptr;
  std::vector<Node> nodes_;
};

// Reference implementation: test every box. Used by tests for cross-checking
// and exposed so the CLI can select it ("use_bvh": false).
bool bruteForceNearest(const std::vector<AABB>& boxes, const Vec3& origin,
                       const Vec3& dir, Hit& out);
std::vector<Hit> bruteForceAll(const std::vector<AABB>& boxes,
                               const Vec3& origin, const Vec3& dir);

// Shared stable sort / sanitizer used by every "all hits" path. Drops any
// non-finite t defensively (they can never arise from validated input, but a
// NaN comparator result would corrupt std::sort, hence the guard).
void sortHits(std::vector<Hit>& hits);

}  // namespace raybox
