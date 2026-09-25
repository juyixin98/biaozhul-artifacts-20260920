// Unit tests for the KD-tree (no framework; assertion-style with counters).
// Covers: brute-force equivalence across deterministic tricky inputs,
// pruning-boundary equality, duplicate coordinates, collinear degeneracy,
// k > size, huge coordinates, radius closed-ball semantics.
#include <algorithm>
#include <cassert>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <iostream>
#include <random>
#include <string>
#include <vector>

#include "kdtree.hpp"

using spatial::Coord;
using spatial::Id;
using spatial::KdTree;
using spatial::Neighbor;
using spatial::Point;

static int g_failed = 0;
static int g_passed = 0;

#define CHECK(cond)                                                       \
  do {                                                                    \
    if (cond) {                                                           \
      ++g_passed;                                                         \
    } else {                                                              \
      ++g_failed;                                                         \
      std::cerr << "FAIL " << __FILE__ << ":" << __LINE__ << " " #cond \
                << "\n";                                                  \
    }                                                                     \
  } while (0)

static std::vector<Neighbor> bruteKnn(const std::vector<Point>& pts,
                                      Coord qx, Coord qy, std::size_t k) {
  std::vector<Neighbor> all;
  for (const auto& p : pts) {
    Coord d2 = (p.x - qx) * (p.x - qx) + (p.y - qy) * (p.y - qy);
    all.push_back({p.id, d2});
  }
  std::sort(all.begin(), all.end(), [](const Neighbor& a, const Neighbor& b) {
    if (a.dist2 != b.dist2) return a.dist2 < b.dist2;
    return a.id < b.id;
  });
  if (k < all.size()) all.resize(k);
  return all;
}

static std::vector<Neighbor> bruteRadius(const std::vector<Point>& pts,
                                         Coord qx, Coord qy, Coord r) {
  std::vector<Neighbor> out;
  Coord r2 = r * r;
  for (const auto& p : pts) {
    Coord d2 = (p.x - qx) * (p.x - qx) + (p.y - qy) * (p.y - qy);
    if (d2 <= r2) out.push_back({p.id, d2});
  }
  std::sort(out.begin(), out.end(), [](const Neighbor& a, const Neighbor& b) {
    if (a.dist2 != b.dist2) return a.dist2 < b.dist2;
    return a.id < b.id;
  });
  return out;
}

static bool same(const std::vector<Neighbor>& a,
                 const std::vector<Neighbor>& b) {
  if (a.size() != b.size()) return false;
  for (std::size_t i = 0; i < a.size(); ++i) {
    if (a[i].id != b[i].id) return false;
    if (a[i].dist2 != b[i].dist2) return false;  // exact: identical arithmetic
  }
  return true;
}

static void fuzzAgainstBrute(const std::vector<Point>& pts,
                             const std::vector<Point>& queries,
                             const std::vector<std::size_t>& ks,
                             const std::vector<Coord>& radii) {
  KdTree tree(pts);
  CHECK(tree.size() == pts.size());
  for (const auto& q : queries) {
    for (auto k : ks) {
      CHECK(same(tree.knn(q.x, q.y, k), bruteKnn(pts, q.x, q.y, k)));
    }
    for (auto r : radii) {
      CHECK(same(tree.radiusSearch(q.x, q.y, r),
                 bruteRadius(pts, q.x, q.y, r)));
    }
  }
}

int main() {
  // Empty / trivial.
  {
    KdTree t(std::vector<Point>{});
    CHECK(t.knn(0, 0, 3).empty());
    CHECK(t.radiusSearch(0, 0, 3).empty());
    KdTree one(std::vector<Point>{{7, 1, 2}});
    auto r = one.knn(1, 2, 5);
    CHECK(r.size() == 1 && r[0].id == 7 && r[0].dist2 == 0);
    CHECK(one.radiusSearch(1, 2, 0).size() == 1);
    CHECK(one.radiusSearch(1, 2, -1).empty());
  }

  // Duplicate coordinates: id tie-break, all distances equal.
  {
    std::vector<Point> pts{{9, 0, 0}, {2, 0, 0}, {5, 0, 0}, {1, 0, 0},
                           {3, 1, 0}};
    KdTree t(pts);
    auto r = t.knn(0, 0, 4);
    CHECK(r.size() == 4);
    CHECK(r[0].id == 1 && r[1].id == 2 && r[2].id == 5 && r[3].id == 9);
    auto all = t.knn(0, 0, 100);  // k > size
    CHECK(all.size() == 5 && all[4].id == 3 && all[4].dist2 == 1);
    auto z = t.radiusSearch(0, 0, 0);
    CHECK(z.size() == 4);
    for (std::size_t i = 1; i < z.size(); ++i) CHECK(z[i - 1].id < z[i].id);
  }

  // Collinear horizontal and vertical: every axis comparison ties somewhere.
  {
    std::vector<Point> pts;
    for (int i = 0; i < 100; ++i) pts.push_back({1000 + i, static_cast<Coord>(i), 4});
    for (int i = 0; i < 100; ++i) pts.push_back({2000 + i, -9, static_cast<Coord>(i)});
    std::vector<Point> q;
    for (int i = -5; i <= 105; i += 7) q.push_back({0, static_cast<Coord>(i), 4});
    for (int i = -5; i <= 105; i += 7) q.push_back({0, -9, static_cast<Coord>(i)});
    q.push_back({0, -9, 4});  // intersection: many equal distances
    fuzzAgainstBrute(pts, q, {0, 1, 2, 8, 150, 205},
                     {0, 1, 3.5L, 10, 1000});
  }

  // Exact-distance boundary: ring at radius 5; prune must include far side.
  {
    std::vector<Point> pts;
    pts.push_back({900, 100, 100});  // far decoy
    pts.push_back({901, 3, 4});
    pts.push_back({902, 4, 3});
    pts.push_back({903, -3, 4});
    pts.push_back({904, -4, -3});
    pts.push_back({905, 5, 0});
    pts.push_back({906, 0, -5});
    KdTree t(pts);
    auto r = t.radiusSearch(0, 0, 5);
    CHECK(r.size() == 6);  // all ring points, decoy excluded
    auto k = t.knn(0, 0, 3);
    CHECK(k.size() == 3);
    // Exact tie: ids must determine order.
    CHECK(k[0].dist2 == 25 && k[0].id == 901);
    CHECK(k[1].id == 902 && k[2].id == 903);
    // Radius just inside excludes the ring.
    CHECK(t.radiusSearch(0, 0, 5 - 1e-12L).empty());
  }

  // Huge integer coordinates: exact distances, no overflow, ties by id.
  {
    std::vector<Point> pts;
    pts.push_back({1, 1'000'000'000'000'000LL, 1'000'000'000'000'000LL});
    pts.push_back({2, 1'000'000'000'000'001LL, 1'000'000'000'000'000LL});
    pts.push_back({3, 1'000'000'000'000'000LL, 1'000'000'000'000'001LL});
    pts.push_back({4, -1'000'000'000'000'000LL, -1'000'000'000'000'000LL});
    KdTree t(pts);
    auto r = t.knn(1'000'000'000'000'000LL, 1'000'000'000'000'000LL, 3);
    CHECK(r[0].id == 1 && r[0].dist2 == 0);
    CHECK(r[1].dist2 == 1 && r[1].id == 2);  // id tie-break between 2 and 3
    CHECK(r[2].id == 3);
    // diagonal separation 2e15 on both axes -> d2 = 8e30, must be finite
    CHECK(std::isfinite((long double)r.size()));
    auto far = t.knn(0, 0, 1);
    CHECK(far[0].id == 1);
  }

  // Rebuild determinism: same points in different insertion order -> same
  // query answers.
  {
    std::mt19937_64 rng(7);
    std::vector<Point> base;
    for (int i = 0; i < 500; ++i)
      base.push_back({i, static_cast<Coord>(rng() % 1000),
                      static_cast<Coord>(rng() % 1000)});
    auto shuffled = base;
    std::shuffle(shuffled.begin(), shuffled.end(), rng);
    KdTree a(base), b(shuffled);
    for (int t = 0; t < 50; ++t) {
      Coord qx = static_cast<Coord>(rng() % 1000);
      Coord qy = static_cast<Coord>(rng() % 1000);
      for (auto k : {1u, 5u, 500u, 999u})
        CHECK(same(a.knn(qx, qy, k), b.knn(qx, qy, k)));
    }
  }

  // Broad randomized equivalence with float coordinates and queries,
  // including k around size boundaries and radii that slice everything.
  {
    std::mt19937 rng(12345);
    std::uniform_real_distribution<double> coord(-1e6, 1e6);
    std::uniform_real_distribution<double> frac(-2e6, 2e6);
    for (int trial = 0; trial < 40; ++trial) {
      int n = 1 + static_cast<int>(rng() % 400);
      std::vector<Point> pts;
      // Sprinkle duplicate coordinates with small probability.
      for (int i = 0; i < n; ++i) {
        if (i > 2 && rng() % 20 == 0) {
          pts.push_back({i, pts[static_cast<std::size_t>(i - 1)].x,
                         pts[static_cast<std::size_t>(i - 1)].y});
        } else if (i > 5 && rng() % 30 == 0) {
          pts.push_back({i, pts[0].x, static_cast<Coord>(i)});  // same x
        } else {
          pts.push_back({i, static_cast<Coord>(coord(rng)),
                         static_cast<Coord>(coord(rng))});
        }
      }
      std::vector<Point> q;
      for (int j = 0; j < 6; ++j)
        q.push_back({0, static_cast<Coord>(frac(rng)),
                     static_cast<Coord>(frac(rng))});
      std::vector<std::size_t> ks{0, 1, 2, static_cast<std::size_t>(n / 2),
                                  static_cast<std::size_t>(n),
                                  static_cast<std::size_t>(n) + 10};
      std::vector<Coord> rs{0, 1, 1e3, 5e4, 5e6};
      fuzzAgainstBrute(pts, q, ks, rs);
    }
  }

  std::cout << "unit tests: " << g_passed << " passed, " << g_failed
            << " failed\n";
  return g_failed ? 1 : 0;
}
