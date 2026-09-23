#include "geometry.hpp"

#include <cmath>
#include <limits>

namespace cps {

std::vector<Point> RemoveConsecutiveDuplicates(
    const std::vector<Point>& pts) {
  std::vector<Point> out;
  out.reserve(pts.size());
  for (const Point& p : pts) {
    if (out.empty() || !(out.back() == p)) {
      out.push_back(p);
    }
  }
  return out;
}

double PointToSegmentDistance(const Point& p, const Point& a,
                              const Point& b) {
  const double abx = b.x - a.x;
  const double aby = b.y - a.y;
  const double apx = p.x - a.x;
  const double apy = p.y - a.y;

  const double len2 = abx * abx + aby * aby;  // |AB|^2

  // 零长度线段（a 与 b 重合）：点到点距离。
  if (!(len2 > 0.0)) {
    return std::hypot(apx, apy);
  }

  // 参数 t = ((P-A)·(B-A)) / |B-A|^2，钳制到 [0,1]。
  // 钳制后的最近点落在闭线段上，这正是“点到线段”而非
  // “点到无限延长线”的关键：垂足在线段之外时取端点。
  double t = (apx * abx + apy * aby) / len2;
  if (t < 0.0) t = 0.0;
  if (t > 1.0) t = 1.0;

  const double cx = a.x + t * abx;  // 线段上的最近点
  const double cy = a.y + t * aby;
  return std::hypot(p.x - cx, p.y - cy);
}

double PointToPolylineDistance(const Point& p,
                               const std::vector<Point>& polyline) {
  if (polyline.empty()) {
    return std::numeric_limits<double>::infinity();
  }
  if (polyline.size() == 1) {
    return std::hypot(p.x - polyline[0].x, p.y - polyline[0].y);
  }
  double best = std::numeric_limits<double>::infinity();
  for (std::size_t i = 0; i + 1 < polyline.size(); ++i) {
    const double d =
        PointToSegmentDistance(p, polyline[i], polyline[i + 1]);
    if (d < best) best = d;
  }
  return best;
}

}  // namespace cps
