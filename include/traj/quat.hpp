// quat.hpp — 双精度三维向量与单位四元数,旋转插值(SLERP)。
//
// 约定:
//   - 四元数布局为 (w, x, y, z),标量在前。
//   - 表示右手坐标系下的旋转;q 与 -q 表示同一旋转(符号等价)。
//   - 所有输出四元数均为单位范数(在 double 精度内重新归一化)。
#pragma once

#include <cmath>
#include <stdexcept>

namespace traj {

constexpr double kPi = 3.14159265358979323846;

struct Vec3 {
  double x = 0.0, y = 0.0, z = 0.0;
};

inline Vec3 lerp(const Vec3& a, const Vec3& b, double u) {
  return {a.x + (b.x - a.x) * u,
          a.y + (b.y - a.y) * u,
          a.z + (b.z - a.z) * u};
}

struct Quat {
  double w = 1.0, x = 0.0, y = 0.0, z = 0.0;
};

inline double dot(const Quat& a, const Quat& b) {
  return a.w * b.w + a.x * b.x + a.y * b.y + a.z * b.z;
}

inline double norm(const Quat& q) { return std::sqrt(dot(q, q)); }

// 归一化;范数低于阈值(退化输入)时抛出 invalid_argument。
inline Quat normalize(const Quat& q) {
  const double n = norm(q);
  if (!(n > 1e-12) || !std::isfinite(n)) {
    throw std::invalid_argument("degenerate quaternion (zero or non-finite norm)");
  }
  const double inv = 1.0 / n;
  return {q.w * inv, q.x * inv, q.y * inv, q.z * inv};
}

inline Quat operator*(double s, const Quat& q) {
  return {s * q.w, s * q.x, s * q.y, s * q.z};
}
inline Quat operator+(const Quat& a, const Quat& b) {
  return {a.w + b.w, a.x + b.x, a.y + b.y, a.z + b.z};
}

// 球面线性插值,u ∈ [0,1]。
//
// 退化与边界处理:
//   1. 符号等价:dot < 0 时翻转 qb,保证走短弧(q 与 -q 同旋转)。
//   2. 小角度:dot > 0.9995(约 < 1.8°)时 sin(theta) 接近 0,SLERP 系数
//      数值不稳定,退化为归一化线性插值(nlerp),误差低于 double 噪声量级。
//   3. 180° 附近:dot ≈ 0,sin(theta) ≈ 1,标准公式天然稳定,无需特判。
//   4. 输出始终重新归一化,保证单位范数。
inline Quat slerp(const Quat& qa_in, const Quat& qb_in, double u) {
  const Quat qa = normalize(qa_in);
  Quat qb = normalize(qb_in);

  double d = dot(qa, qb);
  if (d < 0.0) {  // 符号等价:取短弧
    qb = {-qb.w, -qb.x, -qb.y, -qb.z};
    d = -d;
  }
  if (d > 1.0) d = 1.0;  // 数值保护

  constexpr double kNlerpThreshold = 0.9995;
  if (d > kNlerpThreshold) {  // 小角度退化:nlerp
    return normalize((1.0 - u) * qa + u * qb);
  }

  const double theta = std::acos(d);
  const double s = std::sin(theta);
  const double wa = std::sin((1.0 - u) * theta) / s;
  const double wb = std::sin(u * theta) / s;
  return normalize(wa * qa + wb * qb);
}

// 两个单位四元数之间的旋转角(度),取短弧,范围 [0, 180]。
inline double angleDeg(const Quat& a, const Quat& b) {
  double d = std::fabs(dot(normalize(a), normalize(b)));
  if (d > 1.0) d = 1.0;
  return 2.0 * std::acos(d) * 180.0 / kPi;
}

}  // namespace traj
