// 三维向量、射线、轴对齐包围盒等基础几何类型
// 坐标系：右手笛卡尔坐标系，三轴单位长度一致（无单位约定，由调用方保证）。
#pragma once

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>

namespace geo {

struct Vec3 {
    double x = 0.0;
    double y = 0.0;
    double z = 0.0;

    constexpr Vec3() = default;
    constexpr Vec3(double x_, double y_, double z_) : x(x_), y(y_), z(z_) {}

    double  operator[](int axis) const { return (&x)[axis]; }
    double& operator[](int axis) { return (&x)[axis]; }
};

constexpr Vec3 operator+(const Vec3& a, const Vec3& b) {
    return {a.x + b.x, a.y + b.y, a.z + b.z};
}
constexpr Vec3 operator-(const Vec3& a, const Vec3& b) {
    return {a.x - b.x, a.y - b.y, a.z - b.z};
}
constexpr Vec3 operator-(const Vec3& a) { return {-a.x, -a.y, -a.z}; }
constexpr Vec3 operator*(const Vec3& a, double s) {
    return {a.x * s, a.y * s, a.z * s};
}
constexpr Vec3 operator*(double s, const Vec3& a) { return a * s; }
constexpr Vec3 operator/(const Vec3& a, double s) {
    return {a.x / s, a.y / s, a.z / s};
}

constexpr double dot(const Vec3& a, const Vec3& b) {
    return a.x * b.x + a.y * b.y + a.z * b.z;
}

inline double length(const Vec3& a) {
    return std::sqrt(a.x * a.x + a.y * a.y + a.z * a.z);
}

// 各分量绝对值的最大值（用于尺度相关容差）
inline double compAbsMax(const Vec3& a) {
    return std::max({std::fabs(a.x), std::fabs(a.y), std::fabs(a.z)});
}

inline bool allFinite(const Vec3& a) {
    return std::isfinite(a.x) && std::isfinite(a.y) && std::isfinite(a.z);
}

// 轴对齐包围盒：mn <= mx，允许分量相等（薄片 / 线段 / 点等退化盒）
struct AABB {
    Vec3 mn;
    Vec3 mx;
};

}  // namespace geo
