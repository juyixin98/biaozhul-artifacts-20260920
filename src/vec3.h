// vec3.h — minimal 3D double-precision vector used by the ray/box backend.
//
// Coordinate convention (see README.md):
//   Right-handed Cartesian system, x-right, y-forward, z-up. The intersection
//   math itself is axis-symmetric; only the reported face normal naming
//   assumes this convention. All components are IEEE-754 double.
#pragma once

#include <array>
#include <cmath>

namespace raybox {

struct Vec3 {
  double x = 0.0;
  double y = 0.0;
  double z = 0.0;

  Vec3() = default;
  Vec3(double x_, double y_, double z_) : x(x_), y(y_), z(z_) {}

  double& operator[](int i) {
    // Indexed access keeps the slab loop branchless over the 3 axes.
    return (i == 0) ? x : (i == 1) ? y : z;
  }
  double operator[](int i) const {
    return (i == 0) ? x : (i == 1) ? y : z;
  }

  Vec3 operator+(const Vec3& o) const { return {x + o.x, y + o.y, z + o.z}; }
  Vec3 operator-(const Vec3& o) const { return {x - o.x, y - o.y, z - o.z}; }
  Vec3 operator*(double s) const { return {x * s, y * s, z * s}; }
};

inline Vec3 operator*(double s, const Vec3& v) { return v * s; }

// Returns false for NaN and +/-inf. Input validation rejects any request
// whose coordinate fails this check, so NaN can never enter the sorter.
inline bool isFinite(const Vec3& v) {
  return std::isfinite(v.x) && std::isfinite(v.y) && std::isfinite(v.z);
}

}  // namespace raybox
