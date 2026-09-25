// SPDX-License-Identifier: MIT
// 精确线段相交与分类（整数坐标，有理数输出）。
#pragma once

#include "segi/bigint.hpp"
#include "segi/rational.hpp"

#include <array>
#include <string>
#include <vector>

namespace segi {

struct Point {
    BigInt x;
    BigInt y;
};

struct Segment {
    Point p; // 端点 1
    Point q; // 端点 2
};

// 分类（互斥且穷尽）
enum class RelType {
    DISJOINT,        // 无公共点
    CROSS,           // 恰一个公共点，且该点在两条线段的内部
    ENDPOINT_TOUCH,  // 恰一个公共点，且至少一条线段的公共点是其端点
    COLLINEAR_OVERLAP// 共线且公共部分含无穷多个点（一维区间重叠）
};

const char* relName(RelType t);

// 一个有理点（交点或重叠区间端点），分母恒正、已约分
struct RatPoint {
    Rat x;
    Rat y;
};

// 单点相交时的来源标记（每个维度形如 "p" / "q" / "i"）
struct TouchFlags {
    std::string onA; // 公共点在第一条线段上的角色："p"、"q" 或 "i"(内部)
    std::string onB; // 第二条线段同理
};

struct IntersectionResult {
    RelType type = RelType::DISJOINT;
    bool aZero = false; // 第一条是否为零长线段
    bool bZero = false; // 第二条是否为零长线段

    // CROSS / ENDPOINT_TOUCH：
    RatPoint point;
    TouchFlags flags;

    // COLLINEAR_OVERLAP：
    RatPoint overlapStart; // 重叠区间起点（x 为主，x 相同按 y）
    RatPoint overlapEnd;
};

// 核心判定
IntersectionResult classify(const Segment& a, const Segment& b);

} // namespace segi
