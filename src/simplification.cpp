#include "simplification.hpp"

#include <utility>
#include <vector>

namespace cps {

namespace {

// 在 (lo, hi) 开区间内找到距弦线段 [lo,hi] 最远的点。
// 返回 {下标, 最大距离}；区间为空时返回 {lo, -1}。
// 并列时取下标最小者（从左向右扫描、仅在严格更大时更新）。
std::pair<std::size_t, double> FarthestPoint(
    const std::vector<Point>& pts, std::size_t lo, std::size_t hi) {
  std::size_t idx = lo;
  double max_d = -1.0;
  for (std::size_t i = lo + 1; i < hi; ++i) {
    const double d = PointToSegmentDistance(pts[i], pts[lo], pts[hi]);
    if (d > max_d) {  // 严格大于：并列保留最早的下标
      max_d = d;
      idx = i;
    }
  }
  return {idx, max_d};
}

}  // namespace

SimplifyResult DouglasPeucker(const std::vector<Point>& pts,
                              double epsilon) {
  SimplifyResult result;
  const std::size_t n = pts.size();
  if (n == 0) return result;
  if (n == 1) {
    result.kept = {pts[0]};
    result.kept_indices = {0};
    return result;
  }

  std::vector<char> keep(n, 0);
  keep[0] = 1;
  keep[n - 1] = 1;

  // 显式栈的迭代 DP：每个栈元素是一个待处理区间 [lo, hi]。
  // 处理时找区间内距弦最远的点；若其距离 > epsilon 则标记保留，
  // 并把两个子区间压栈。处理顺序不影响最终结果，
  // 但“并列取最小下标”的规则使输出与任何处理顺序无关。
  std::vector<std::pair<std::size_t, std::size_t>> stack;
  stack.push_back({0, n - 1});

  while (!stack.empty()) {
    const auto [lo, hi] = stack.back();
    stack.pop_back();
    if (hi <= lo + 1) continue;

    const auto [idx, max_d] = FarthestPoint(pts, lo, hi);
    if (max_d > epsilon) {
      keep[idx] = 1;
      stack.push_back({lo, idx});
      stack.push_back({idx, hi});
    }
  }

  result.kept.reserve(n);
  result.kept_indices.reserve(n);
  for (std::size_t i = 0; i < n; ++i) {
    if (keep[i]) {
      result.kept.push_back(pts[i]);
      result.kept_indices.push_back(i);
    }
  }
  return result;
}

}  // namespace cps
