// simplification.hpp - Douglas-Peucker 折线简化
#pragma once

#include <vector>

#include "geometry.hpp"

namespace cps {

struct SimplifyResult {
  std::vector<Point> kept;  // 简化后的折线，保持原始顺序，端点保留
  // 每个被保留点在“已去重输入”中的下标（供调试 / 审计）。
  std::vector<std::size_t> kept_indices;
};

// Douglas-Peucker 简化。
//
// 约定：
//   - 阈值 epsilon >= 0。
//   - 分裂判据使用“点到弦线段 [first,last] 的距离”（含端点钳制），
//     而不是到无限延长线的垂直距离。因此误差界为
//     max_i dist(P_i, 简化折线) <= epsilon（数值舍入误差范围内），
//     该结论对回折/自交折线同样成立（逐段 DP 不变量，见 README）。
//   - 距离严格大于 epsilon 的点才触发分裂（dist > epsilon）；
//     恰好相等的点不保留。epsilon = 0 时只有真正落在
//     弦线段上（计算距离 == 0）的点才被丢弃，共线点被去除。
//   - 多个点同为最大距离时，保留沿折线方向最先出现（下标最小）者，
//     保证结果确定（与浮点运算顺序无关的固定规则）。
//   - 端点恒保留。输入应由调用方先去除相邻重复点。
SimplifyResult DouglasPeucker(const std::vector<Point>& pts, double epsilon);

}  // namespace cps
