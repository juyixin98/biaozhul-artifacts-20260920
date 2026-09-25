// test_raybox.cc — unit tests for ray/AABB intersection and BVH queries.
//
// Covers the acceptance matrix:
//   origin inside box, face/edge/corner grazing, negative-direction rays,
//   degenerate boxes (slab / column / point), zero direction components,
//   nearest and all-hit modes, BVH vs brute-force agreement, and random
//   fuzz comparison.
#include <cmath>
#include <cstdio>
#include <random>
#include <string>
#include <vector>

#include "../src/bvh.h"
#include "../src/ray_aabb.h"
#include "../src/vec3.h"

using namespace raybox;

namespace {

int g_failures = 0;
int g_checks = 0;

void check(bool cond, const std::string& msg) {
  ++g_checks;
  if (!cond) {
    ++g_failures;
    std::printf("  FAIL: %s\n", msg.c_str());
  }
}

bool nearEq(double a, double b, double tol = 1e-9) {
  return std::fabs(a - b) <= tol * (1.0 + std::fabs(a) + std::fabs(b));
}

bool nearV(const Vec3& a, const Vec3& b, double tol = 1e-9) {
  return nearEq(a.x, b.x, tol) && nearEq(a.y, b.y, tol) &&
         nearEq(a.z, b.z, tol);
}

AABB box(double x0, double y0, double z0, double x1, double y1,
         double z1) {
  return AABB{Vec3(x0, y0, z0), Vec3(x1, y1, z1)};
}

bool hitOnce(const Vec3& o, const Vec3& d, const AABB& b, Hit& h) {
  return intersectRayAABB(o, d, b, 0, h);
}

// ---------------------------------------------------------------------------

void testBasicPositive() {
  std::printf("[test] basic positive-direction hit\n");
  AABB b = box(0, 0, 0, 2, 2, 2);
  Hit h;
  check(hitOnce(Vec3(-2, 1, 1), Vec3(1, 0, 0), b, h), "ray should hit");
  check(nearEq(h.t, 2.0), "entry t == 2");
  check(nearEq(h.exit_t, 4.0), "exit t == 4");
  check(nearV(h.point, Vec3(0, 1, 1)), "entry point");
  check(nearV(h.normal, Vec3(-1, 0, 0)), "entry normal points -x");

  check(!hitOnce(Vec3(-2, 3, 1), Vec3(1, 0, 0), b, h), "ray above box misses");
  check(!hitOnce(Vec3(3, 1, 1), Vec3(1, 0, 0), b, h), "ray behind misses");
}

void testOriginInside() {
  std::printf("[test] origin inside / on the box\n");
  AABB b = box(0, 0, 0, 2, 2, 2);
  Hit h;
  // Interior origin: t == 0, zero normal.
  check(hitOnce(Vec3(1, 1, 1), Vec3(1, 0, 0), b, h), "inside: hit");
  check(h.t == 0.0, "inside: t == 0");
  check(nearV(h.normal, Vec3(0, 0, 0)), "inside: zero normal");
  check(nearV(h.point, Vec3(1, 1, 1)), "inside: point == origin");
  check(nearEq(h.exit_t, 1.0), "inside: exit through +x");

  // Origin on a face, ray pointing inward: t == 0 as well (closed box).
  check(hitOnce(Vec3(0, 1, 1), Vec3(1, 0, 0), b, h), "on face inward: hit");
  check(h.t == 0.0, "on face inward: t == 0");

  // Origin on a face, ray pointing outward: a boundary touch at t == 0.
  check(hitOnce(Vec3(0, 1, 1), Vec3(-1, 0, 0), b, h), "on face outward: hit");
  check(h.t == 0.0, "on face outward: t == 0");
}

void testGrazing() {
  std::printf("[test] grazing: face plane, edge and corner\n");
  AABB b = box(0, 0, 0, 2, 2, 2);
  Hit h;

  // Face graze: ray lies in the z == 0 plane along the box.
  check(hitOnce(Vec3(-1, 1, 0), Vec3(1, 0, 0), b, h), "face graze is a hit");
  check(nearEq(h.t, 1.0), "face graze entry t");
  check(nearEq(h.exit_t, 3.0), "face graze exit t");

  // Edge graze: ray along the line y == 0, z == 0.
  check(hitOnce(Vec3(-1, 0, 0), Vec3(1, 0, 0), b, h), "edge graze is a hit");
  check(nearEq(h.t, 1.0), "edge graze entry t");

  // Corner graze through (0,0,0) along the body diagonal, direction with
  // three non-zero components and a negative component.
  Vec3 o(-1, -1, -1), d(1, 1, 1);
  check(hitOnce(o, d, b, h), "corner graze is a hit");
  check(nearEq(h.t, 1.0), "corner graze t == 1");
  check(nearV(h.point, Vec3(0, 0, 0)), "corner graze point");

  // Slightly outside the graze plane: miss.
  check(!hitOnce(Vec3(-1, 1, -1e-8), Vec3(1, 0, 0), b, h),
        "just below face: miss");
}

void testNegativeDirection() {
  std::printf("[test] negative / mixed-sign direction components\n");
  AABB b = box(0, 0, 0, 2, 2, 2);
  Hit h;

  // Ray traveling in -x enters through the high face; normal +x.
  check(hitOnce(Vec3(4, 1, 1), Vec3(-1, 0, 0), b, h), "neg x: hit");
  check(nearEq(h.t, 2.0), "neg x: t == 2");
  check(nearV(h.point, Vec3(2, 1, 1)), "neg x: entry at high face");
  check(nearV(h.normal, Vec3(1, 0, 0)), "neg x: normal +x");
  check(nearEq(h.exit_t, 4.0), "neg x: exit");

  // Diagonal with mixed signs, starting far at (+,+ ,-).
  Vec3 o(5, 5, -3), d(-1, -1, 1);
  check(hitOnce(o, d, b, h), "diagonal mixed: hit");
  check(nearEq(h.t, 3.0), "diagonal mixed: t == 3");
  check(nearV(h.point, Vec3(2, 2, 0)), "diagonal mixed: entry corner");

  // Ray pointing away behind the box misses.
  check(!hitOnce(Vec3(4, 1, 1), Vec3(1, 0, 0), b, h), "pointing away: miss");
}

void testZeroDirComponent() {
  std::printf("[test] zero direction components handled separately\n");
  AABB b = box(0, 0, 0, 2, 2, 2);
  Hit h;

  // dir = (1,0,0), origin outside the y/z slab: miss, no divide by zero.
  check(!hitOnce(Vec3(-2, 3, 1), Vec3(1, 0, 0), b, h),
        "parallel outside y slab: miss");
  check(!hitOnce(Vec3(-2, 1, -1), Vec3(1, 0, 0), b, h),
        "parallel outside z slab: miss");

  // dir with a zero component but within the slab: hit.
  check(hitOnce(Vec3(-2, 1, 1), Vec3(1, 0, 0), b, h),
        "parallel in slab: hit");
  check(nearEq(h.t, 2.0), "parallel in slab: t");

  // Two zero components, aligned with an edge; origin outside box.
  check(hitOnce(Vec3(-1, 0, 0), Vec3(1, 0, 0), b, h),
        "two zero dirs on the edge line: graze hit");
  check(!hitOnce(Vec3(-1, -1e-6, 0), Vec3(1, 0, 0), b, h),
        "two zero dirs off the edge line: miss");
}

void testDegenerateBox() {
  std::printf("[test] degenerate boxes: slab, column, point\n");
  Hit h;

  // Flat slab z extent zero at z == 1.
  AABB slab = box(0, 0, 1, 2, 2, 1);
  check(hitOnce(Vec3(1, 1, -2), Vec3(0, 0, 1), slab, h), "slab: hit");
  check(nearEq(h.t, 3.0), "slab: t == 3");
  check(nearEq(h.exit_t, 3.0), "slab: zero-thickness, t == exit_t");
  check(nearV(h.normal, Vec3(0, 0, -1)), "slab: entry normal -z");
  // Parallel ray lying in the slab plane: meets the rectangle.
  check(hitOnce(Vec3(-1, 1, 1), Vec3(1, 0, 0), slab, h),
        "slab: in-plane ray through: graze hit");
  check(!hitOnce(Vec3(-1, 3, 1), Vec3(1, 0, 0), slab, h),
        "slab: in-plane ray outside: miss");

  // Column: x and y extents zero.
  AABB column = box(1, 1, 0, 1, 1, 2);
  check(hitOnce(Vec3(1, 1, -2), Vec3(0, 0, 1), column, h),
        "column: aligned ray hits");
  check(nearEq(h.t, 2.0), "column: t == 2");
  check(!hitOnce(Vec3(1, 1.1, -2), Vec3(0, 0, 1), column, h),
        "column: offset ray misses");

  // Single point at (1,1,1).
  AABB point = box(1, 1, 1, 1, 1, 1);
  check(hitOnce(Vec3(-1, -1, -1), Vec3(1, 1, 1), point, h),
        "point: diagonal ray through the point hits");
  check(nearEq(h.t, 2.0), "point: t == 2");
  check(nearEq(h.exit_t, 2.0), "point: t == exit_t");
  check(!hitOnce(Vec3(0, 0, 0), Vec3(1, 1, 0), point, h),
        "point: ray not aimed at point misses");
}

void testNaNAndFiniteGuards() {
  std::printf("[test] no NaN reaches sorting; hits sanitized\n");
  // Normal geometry produces only finite values.
  std::vector<AABB> boxes = {box(0, 0, 0, 1, 1, 1),
                             box(2, 2, 2, 3, 3, 3),
                             box(-5, -5, -5, -4, -4, -4)};
  std::vector<Hit> hits = bruteForceAll(boxes, Vec3(0.5, 0.5, -3),
                                        Vec3(0, 0, 1));
  bool allFinite = true;
  for (const Hit& h : hits) {
    if (!std::isfinite(h.t) || !std::isfinite(h.exit_t)) allFinite = false;
  }
  check(allFinite, "all reported t values finite");

  // sortHits must tolerate a poisoned record by dropping it instead of
  // corrupting the order (defensive guarantee).
  std::vector<Hit> poisoned;
  Hit bad;
  bad.id = 99;
  bad.t = std::nan("");
  poisoned.push_back(bad);
  Hit good;
  good.id = 1;
  good.t = 1.0;
  poisoned.push_back(good);
  sortHits(poisoned);
  check(poisoned.size() == 1 && poisoned[0].id == 1,
        "NaN-hit dropped before sort, good hit retained");
}

void testNearestAndAll() {
  std::printf("[test] nearest vs all-hit ordering\n");
  std::vector<AABB> boxes;
  boxes.push_back(box(10, -1, -1, 11, 1, 1));  // far
  boxes.push_back(box(2, -1, -1, 3, 1, 1));    // near
  boxes.push_back(box(5, -1, -1, 6, 1, 1));    // middle
  BVH tree(boxes);

  Hit h;
  check(tree.nearest(Vec3(0, 0, 0), Vec3(1, 0, 0), h), "nearest: hit");
  check(h.id == 1, "nearest: box id 1 (t == 2)");
  check(nearEq(h.t, 2.0), "nearest: t == 2");

  std::vector<Hit> all = tree.allHits(Vec3(0, 0, 0), Vec3(1, 0, 0));
  check(all.size() == 3, "all: three hits");
  bool ordered = all.size() == 3;
  for (size_t i = 1; i < all.size(); ++i)
    if (all[i - 1].t > all[i].t) ordered = false;
  check(ordered, "all: ascending t");
  check(all[0].id == 1 && all[1].id == 2 && all[2].id == 0,
        "all: order near, middle, far");

  // Nested boxes: nearest is the smaller inner one.
  std::vector<AABB> nested = {box(-2, -2, -2, 2, 2, 2),
                              box(-1, -1, -1, 1, 1, 1)};
  BVH tree2(nested);
  check(tree2.nearest(Vec3(-5, 0, 0), Vec3(1, 0, 0), h) && h.id == 0,
        "nested: outer box hit first (its face is closer)");
  std::vector<Hit> nh = tree2.allHits(Vec3(-5, 0, 0), Vec3(1, 0, 0));
  check(nh.size() == 2, "nested: both boxes reported");
}

void testBVHvsBruteForce() {
  std::printf("[test] BVH and brute force agree (fixed scenarios)\n");
  std::vector<AABB> scenes[4];
  scenes[0] = {box(0, 0, 0, 1, 1, 1)};
  scenes[1] = {box(0, 0, 0, 2, 2, 2), box(3, 3, 3, 4, 4, 4),
               box(-1, -1, 2, 1, 1, 3), box(1.5, -2, -2, 2.5, 0, 0)};
  // Many overlapping boxes.
  for (int i = 0; i < 31; ++i)
    scenes[2].push_back(box(i * 0.25, -1.0, -1.0, i * 0.25 + 1.0, 1.0, 1.0));
  // Identical boxes: stress tie handling.
  for (int i = 0; i < 8; ++i)
    scenes[3].push_back(box(0, 0, 0, 1, 1, 1));

  Vec3 origins[] = {Vec3(-3, 0, 0), Vec3(1, 1, -5), Vec3(0.5, 0.5, 0.5),
                    Vec3(2, -3, 0.5), Vec3(-1, 2, 2)};
  Vec3 dirs[] = {Vec3(1, 0, 0), Vec3(0, 0, 1), Vec3(1, 1, 1),
                 Vec3(-1, 2, 0.5), Vec3(0.3, -0.7, 1)};

  for (size_t s = 0; s < 4; ++s) {
    BVH tree(scenes[s]);
    for (const Vec3& o : origins) {
      for (const Vec3& d0 : dirs) {
        Vec3 d = d0;  // dirs already unit-ish / non-zero
        Hit bn, nn;
        bool bHit = bruteForceNearest(scenes[s], o, d, bn);
        bool nHit = tree.nearest(o, d, nn);
        check(bHit == nHit, "scenario: nearest hit flag agrees");
        if (bHit && nHit) {
          check(bn.id == nn.id, "scenario: nearest id agrees");
          check(nearEq(bn.t, nn.t, 1e-12), "scenario: nearest t agrees");
        }
        std::vector<Hit> ba = bruteForceAll(scenes[s], o, d);
        std::vector<Hit> na = tree.allHits(o, d);
        check(ba.size() == na.size(), "scenario: all-hit counts agree");
        size_t n = std::min(ba.size(), na.size());
        for (size_t i = 0; i < n; ++i) {
          check(ba[i].id == na[i].id, "scenario: hit id order agrees");
          check(nearEq(ba[i].t, na[i].t, 1e-12),
                "scenario: hit t agrees");
        }
      }
    }
  }
}

void testFuzz() {
  std::printf("[test] randomized fuzz: BVH vs brute force\n");
  std::mt19937_64 rng(0xC0FFEEULL);
  std::uniform_real_distribution<double> uni(-5.0, 5.0);
  std::uniform_int_distribution<int> nDist(1, 40);

  int mismatch = 0;
  const int trials = 4000;
  for (int trial = 0; trial < trials; ++trial) {
    int n = nDist(rng);
    std::vector<AABB> boxes;
    for (int i = 0; i < n; ++i) {
      Vec3 a(uni(rng), uni(rng), uni(rng));
      Vec3 span(uni(rng) * 0.8, uni(rng) * 0.8, uni(rng) * 0.8);
      // span in [-4,4]; force non-negative and occasionally zero (degenerate).
      Vec3 mn, mx;
      for (int ax = 0; ax < 3; ++ax) {
        double s = span[ax];
        if (rng() % 20 == 0) s = 0.0;  // 5% chance of zero extent per axis
        double lo = a[ax];
        double hi = a[ax] + std::fabs(s);
        mn[ax] = std::min(lo, hi);
        mx[ax] = std::max(lo, hi);
      }
      boxes.push_back(AABB{mn, mx});
    }

    Vec3 origin(uni(rng) * 1.4, uni(rng) * 1.4, uni(rng) * 1.4);
    Vec3 d;
    // Occasionally zero some direction components deliberately.
    for (int ax = 0; ax < 3; ++ax) {
      int r = rng() % 8;
      d[ax] = (r == 0) ? 0.0 : uni(rng);
    }
    if (d.x == 0.0 && d.y == 0.0 && d.z == 0.0) d.x = 1.0;

    BVH tree(boxes);
    Hit bn, nn;
    bool bHit = bruteForceNearest(boxes, origin, d, bn);
    bool nHit = tree.nearest(origin, d, nn);
    if (bHit != nHit || (bHit && (bn.id != nn.id ||
                                  !nearEq(bn.t, nn.t, 1e-10)))) {
      ++mismatch;
    }
    std::vector<Hit> ba = bruteForceAll(boxes, origin, d);
    std::vector<Hit> na = tree.allHits(origin, d);
    if (ba.size() != na.size()) {
      ++mismatch;
      continue;
    }
    for (size_t i = 0; i < ba.size(); ++i) {
      if (ba[i].id != na[i].id || !nearEq(ba[i].t, na[i].t, 1e-10)) {
        ++mismatch;
        break;
      }
    }
  }
  check(mismatch == 0,
        "fuzz: 4000 random scenes, zero BVH/brute-force mismatches"
        " (mismatches=" +
            std::to_string(mismatch) + ")");
}

}  // namespace

int main() {
  testBasicPositive();
  testOriginInside();
  testGrazing();
  testNegativeDirection();
  testZeroDirComponent();
  testDegenerateBox();
  testNaNAndFiniteGuards();
  testNearestAndAll();
  testBVHvsBruteForce();
  testFuzz();

  std::printf("\n%d checks, %d failures\n", g_checks, g_failures);
  return g_failures == 0 ? 0 : 1;
}
