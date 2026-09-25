// Static 2D KD-tree with exact deterministic nearest-neighbour semantics.
//
// Coordinate system
// -----------------
// Points live on a 2D Cartesian plane (NOT geographic longitude/latitude).
// Distances are Euclidean: d(p,q)^2 = (px-qx)^2 + (py-qy)^2. All arithmetic
// uses long double (80-bit extended precision on x86-64 with x87/SSE mixing
// avoided via -ffloat-store-free scalar math); differences of coordinates as
// large as +/-1e15 can be squared without overflow (range reaches ~8e30,
// comfortably below LDBL_MAX ~= 1.18e4932).
//
// Tie handling
// ------------
// The global order is (distance^2 ascending, id ascending). Equal-distance
// ties are broken by the caller-supplied integer id, never by heap or memory
// layout. Partitioning uses the same kind of total order on (axis coord, other
// coord, id) so duplicate coordinates partition deterministically.
//
// Pruning at the boundary (the subtle part)
// -----------------------------------------
// KNN: the far half is pruned ONLY when planeDist^2 is STRICTLY greater than
// the squared distance of the worst retained candidate. At equality the far
// half is searched: the bounding ball of radius sqrt(worstDist2) touches the
// split plane, and points at that contact distance with a smaller id could
// replace the current worst candidate.
// Radius: the far half is searched whenever planeDist^2 <= r^2 — the ball is
// closed, so points exactly on the boundary belong to the result.
#pragma once

#include <algorithm>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <functional>
#include <limits>
#include <numeric>
#include <queue>
#include <vector>

namespace spatial {

using Coord = long double;
using Id = std::int64_t;

struct Point {
  Id id = 0;
  Coord x = 0.0L;
  Coord y = 0.0L;
};

struct Neighbor {
  Id id = 0;
  Coord dist2 = 0.0L;  // squared Euclidean distance
};

namespace detail {

// Total order along an axis; deterministic even for duplicate coordinates.
struct AxisLess {
  int axis;
  bool operator()(const Point& a, const Point& b) const {
    if (axis == 0) {
      if (a.x != b.x) return a.x < b.x;
      if (a.y != b.y) return a.y < b.y;
    } else {
      if (a.y != b.y) return a.y < b.y;
      if (a.x != b.x) return a.x < b.x;
    }
    return a.id < b.id;
  }
};

// (distance^2, id) order shared by the heap and final sorting.
struct DistIdLess {
  bool operator()(const Neighbor& a, const Neighbor& b) const {
    if (a.dist2 != b.dist2) return a.dist2 < b.dist2;
    return a.id < b.id;
  }
};

}  // namespace detail

class KdTree {
 public:
  KdTree() = default;
  explicit KdTree(std::vector<Point> points) { build(std::move(points)); }

  std::size_t size() const noexcept { return points_.size(); }
  const std::vector<Point>& points() const noexcept { return points_; }

  void build(std::vector<Point> points) {
    points_ = std::move(points);
    nodes_.clear();
    nodes_.reserve(points_.size());
    root_ = kNull;
    if (points_.empty()) return;
    std::vector<std::size_t> idx(points_.size());
    std::iota(idx.begin(), idx.end(), std::size_t{0});
    root_ = buildRec(idx, 0, idx.size(), 0);
  }

  // k nearest neighbours. k > point count returns every point; k == 0 returns
  // none. Result is sorted by (distance^2, id).
  std::vector<Neighbor> knn(Coord qx, Coord qy, std::size_t k) const {
    if (k == 0 || points_.empty()) return {};
    if (k > points_.size()) k = points_.size();

    // Max-heap: top is the worst retained candidate (largest dist2, then id).
    std::priority_queue<Neighbor, std::vector<Neighbor>, detail::DistIdLess> heap;
    knnAt(root_, 0, qx, qy, k, heap);

    std::vector<Neighbor> out;
    out.reserve(heap.size());
    while (!heap.empty()) {
      out.push_back(heap.top());
      heap.pop();
    }
    std::reverse(out.begin(), out.end());  // worst-first popped -> ascending
    return out;
  }

  // All points with distance <= radius (closed ball). Sorted by (dist2, id).
  // A negative radius yields an empty result.
  std::vector<Neighbor> radiusSearch(Coord qx, Coord qy, Coord radius) const {
    std::vector<Neighbor> out;
    if (points_.empty() || radius < 0) return out;
    const Coord r2 = radius * radius;
    radiusRec(root_, 0, qx, qy, r2, out);
    std::sort(out.begin(), out.end(), [](const Neighbor& a, const Neighbor& b) {
      if (a.dist2 != b.dist2) return a.dist2 < b.dist2;
      return a.id < b.id;
    });
    return out;
  }

 private:
  static constexpr std::size_t kNull = std::numeric_limits<std::size_t>::max();

  struct Node {
    Point p;
    std::size_t left = kNull;
    std::size_t right = kNull;
  };

  // Partition idx[lo,hi) around the median under the axis total order; the
  // median slot becomes the node, the two halves become the subtrees. Nodes
  // are emitted in pre-order for cache-friendly DFS.
  std::size_t buildRec(std::vector<std::size_t>& idx, std::size_t lo,
                       std::size_t hi, int depth) {
    const std::size_t n = hi - lo;
    const int axis = depth & 1;
    const std::size_t midLocal = n / 2;  // consistent right-biased median

    std::nth_element(
        idx.begin() + static_cast<std::ptrdiff_t>(lo),
        idx.begin() + static_cast<std::ptrdiff_t>(lo + midLocal),
        idx.begin() + static_cast<std::ptrdiff_t>(hi),
        [&](std::size_t ia, std::size_t ib) {
          return detail::AxisLess{axis}(points_[ia], points_[ib]);
        });

    const std::size_t here = nodes_.size();
    nodes_.push_back(Node{points_[idx[lo + midLocal]], kNull, kNull});
    if (midLocal > 0) {
      nodes_[here].left = buildRec(idx, lo, lo + midLocal, depth + 1);
    }
    if (lo + midLocal + 1 < hi) {
      nodes_[here].right = buildRec(idx, lo + midLocal + 1, hi, depth + 1);
    }
    return here;
  }

  static Coord sqr(Coord v) { return v * v; }

  void knnAt(std::size_t ni, int depth, Coord qx, Coord qy, std::size_t k,
             std::priority_queue<Neighbor, std::vector<Neighbor>,
                                 detail::DistIdLess>& heap) const {
    if (ni == kNull) return;
    const Node& node = nodes_[ni];
    const int axis = depth & 1;
    const Coord qv = axis == 0 ? qx : qy;
    const Coord nv = axis == 0 ? node.p.x : node.p.y;
    const Coord diff = qv - nv;

    const std::size_t nearChild = diff < 0 ? node.left : node.right;
    const std::size_t farChild = diff < 0 ? node.right : node.left;

    knnAt(nearChild, depth + 1, qx, qy, k, heap);

    // The node sits on the split plane; evaluate it before pruning so a
    // boundary-contact candidate is never skipped.
    const Coord d2 = sqr(node.p.x - qx) + sqr(node.p.y - qy);
    const Neighbor cand{node.p.id, d2};
    if (heap.size() < k) {
      heap.push(cand);
    } else if (detail::DistIdLess{}(cand, heap.top())) {
      // Strictly better than the current worst. (Equal (dist2,id) cannot occur
      // with unique ids; an equal-dist2/larger-id candidate is correctly
      // discarded.)
      heap.pop();
      heap.push(cand);
    }

    // Strict inequality on the prune side: equality must still be searched.
    if (heap.size() < k || sqr(diff) <= heap.top().dist2) {
      knnAt(farChild, depth + 1, qx, qy, k, heap);
    }
  }

  void radiusRec(std::size_t ni, int depth, Coord qx, Coord qy, Coord r2,
                 std::vector<Neighbor>& out) const {
    if (ni == kNull) return;
    const Node& node = nodes_[ni];
    const int axis = depth & 1;
    const Coord qv = axis == 0 ? qx : qy;
    const Coord nv = axis == 0 ? node.p.x : node.p.y;
    const Coord diff = qv - nv;

    const std::size_t nearChild = diff < 0 ? node.left : node.right;
    const std::size_t farChild = diff < 0 ? node.right : node.left;
    radiusRec(nearChild, depth + 1, qx, qy, r2, out);

    const Coord d2 = sqr(node.p.x - qx) + sqr(node.p.y - qy);
    if (d2 <= r2) out.push_back(Neighbor{node.p.id, d2});  // closed ball

    if (sqr(diff) <= r2) {  // closed interval: boundary belongs to the search
      radiusRec(farChild, depth + 1, qx, qy, r2, out);
    }
  }

  std::vector<Point> points_;
  std::vector<Node> nodes_;
  std::size_t root_ = kNull;
};

}  // namespace spatial
