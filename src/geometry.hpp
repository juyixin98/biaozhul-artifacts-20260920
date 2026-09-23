// geometry.hpp - 平面点、点到线段 / 点到折线的距离计算
//
// 坐标约定：二维平面笛卡尔坐标 (x, y)，double 精度（IEEE 754 binary64，
// 约 15-17 位有效十进制数字）。本模块不做任何地图投影，不假设单位是米，
// 距离与输入坐标同单位。
#pragma once

#include <cstddef>
#include <vector>

namespace cps {

struct Point {
  double x = 0.0;
  double y = 0.0;
  bool operator==(const Point& other) const {
    // 位级一致：NaN != NaN（输入侧已拒绝 NaN/Inf，这里保持严格语义）
    return x == other.x && y == other.y;
  }
};

// 删除相邻重复点（只比较直接相邻的点，不删除非相邻的重复点）。
// 用于在简化前消除零长度线段；端点只要与最后一个保留点不同就会被保留。
std::vector<Point> RemoveConsecutiveDuplicates(const std::vector<Point>& pts);

// 点 p 到线段 [a, b] 的欧氏距离。
// 退化情形：a == b（零长度线段）时退化为点到点距离。
double PointToSegmentDistance(const Point& p, const Point& a, const Point& b);

// 点 p 到折线 polyline 的距离 = 到所有线段距离的最小值。
// 退化情形：折线为 1 个点时为点到点距离；为空时返回 +inf
// （调用方应保证不传入空折线）。
double PointToPolylineDistance(const Point& p,
                               const std::vector<Point>& polyline);

}  // namespace cps
