// geometry.hpp — 整数坐标线段相交判定、分类与精确交点计算。
//
// 坐标系：二维笛卡尔平面，x 轴向右、y 轴向上，单位任意；无地理投影。
// 精度模型：
//  - 输入坐标为 64 位有符号整数，且要求 |x|, |y| <= MAX_COORD = 2^62 - 2。
//  - 所有定向测试（叉积）使用 signed __int128（约 [-2^127, 2^127)）：
//      坐标差 |dx| <= 2 * MAX_COORD + 1 < 2^63，
//      叉积 |dx1*dy2 - dy1*dx2| < 2^127，故不溢出。
//  - 交点的分子可能远超 128 位，坐标计算改用 cpp_int 任意精度整数；
//      输出为最简分数，全流程无浮点运算。
#pragma once

#include <cstdint>
#include <string>
#include <vector>

#include "rational.hpp"

namespace segint {

// 允许的输入坐标绝对值上界（含）。
constexpr int64_t MAX_COORD = (int64_t(1) << 62) - 2;

struct Point {
    int64_t x = 0;
    int64_t y = 0;
};

// 端点名称，用于命中标记。
enum class EndId { A, B, C, D };

inline const char* end_name(EndId e) {
    switch (e) {
        case EndId::A: return "a";
        case EndId::B: return "b";
        case EndId::C: return "c";
        case EndId::D: return "d";
    }
    return "?";
}

// 精确交点（分数坐标）及落在该点上的端点。
struct ContactPoint {
    Rational x;
    Rational y;
    std::vector<std::string> hits;  // {"a"} / {"a","c"} / {} （真交叉时为空）
};

// 相交分类。
//   none    ：不相交（含共线但无重叠）
//   cross   ：唯一交点且位于两条线段的内部（真交叉）
//   touch   ：唯一交点且至少涉及一个端点（端点接触；退化线段的接触也归此类）
//   overlap ：两条非退化线段共线且共享一段长度为正的区间
enum class Classify { None, Cross, Touch, Overlap };

const char* classify_name(Classify c);

struct Result {
    Classify cls = Classify::None;
    // cross / touch：恰一个点；overlap：两个点（区间端点，顺序与坐标相关）；none：空。
    std::vector<ContactPoint> points;
};

// 坐标是否在允许范围内。
bool coord_in_range(int64_t v);

// 计算线段 ab 与 cd 的相交分类。不做范围检查（由入口层校验）。
Result intersect(const Point& a, const Point& b,
                 const Point& c, const Point& d);

}  // namespace segint
