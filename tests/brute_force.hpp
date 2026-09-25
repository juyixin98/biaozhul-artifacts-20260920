// brute_force.hpp — 小网格逐格参考实现（仅用于测试，独立于扫描线算法）
//
// 将并集离散为单位格 [x,x+1) × [y,y+1)，逐格判定是否被任一矩形覆盖：
//   面积      = 被覆盖单位格数量；
//   周长      = 被覆盖格的四条边中、邻格（或外界）未被覆盖的边数。
// 该定义与半开语义一致：仅边界贴合的矩形不共享面积，但公共边两侧均被覆盖，
// 故不会计入周长。
#pragma once

#include <algorithm>
#include <cstdint>
#include <vector>

#include "../src/geometry.hpp"

namespace rectunion {
namespace test {

struct BruteMetrics {
    unsigned long long area;
    unsigned long long perimeter;
};

inline BruteMetrics bruteForce(const std::vector<Rect>& rects) {
    long long minX = 0, minY = 0, maxX = 0, maxY = 0;
    bool any = false;
    for (const Rect& r : rects) {
        if (r.x1 == r.x2 || r.y1 == r.y2) continue;
        if (!any) {
            minX = r.x1; maxX = r.x2; minY = r.y1; maxY = r.y2;
            any = true;
        } else {
            minX = std::min(minX, r.x1);
            maxX = std::max(maxX, r.x2);
            minY = std::min(minY, r.y1);
            maxY = std::max(maxY, r.y2);
        }
    }
    if (!any) return {0, 0};

    const int W = static_cast<int>(maxX - minX);
    const int H = static_cast<int>(maxY - minY);
    std::vector<unsigned char> covered(static_cast<size_t>(W) * H, 0);

    auto at = [&](int x, int y) -> unsigned char& {
        return covered[static_cast<size_t>(y) * W + x];
    };

    for (const Rect& r : rects) {
        if (r.x1 == r.x2 || r.y1 == r.y2) continue;
        for (long long x = r.x1; x < r.x2; ++x)
            for (long long y = r.y1; y < r.y2; ++y)
                at(static_cast<int>(x - minX), static_cast<int>(y - minY)) = 1;
    }

    unsigned long long area = 0;
    for (unsigned char c : covered) area += c;

    unsigned long long perimeter = 0;
    for (int y = 0; y < H; ++y) {
        for (int x = 0; x < W; ++x) {
            if (!at(x, y)) continue;
            // 上、下、左、右四条边：邻格不在包围盒内或未被覆盖则计入。
            if (y + 1 >= H || !at(x, y + 1)) ++perimeter;
            if (y - 1 <  0 || !at(x, y - 1)) ++perimeter;
            if (x - 1 <  0 || !at(x - 1, y)) ++perimeter;
            if (x + 1 >= W || !at(x + 1, y)) ++perimeter;
        }
    }
    return {area, perimeter};
}

} // namespace test
} // namespace rectunion
