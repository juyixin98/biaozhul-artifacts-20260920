// SE(2) pose utilities. A pose is (x, y, theta_rad), theta in (-pi, pi]
// as far as reporting is concerned; internally differences are always wrapped.
#ifndef PGO_SE2_HPP
#define PGO_SE2_HPP

#include <cmath>

namespace pgo {

inline constexpr double kPi = 3.14159265358979323846;

// Wrap any angle into [-pi, pi] (atan2 convention; +/-pi are identical poses).
template <typename T>
inline T normalizeAngle(T a) {
  // atan2(sin, cos) is branch-free and works with Ceres Jets as well as double.
  using std::atan2;
  using std::cos;
  using std::sin;
  T wrapped = atan2(sin(a), cos(a));
  return wrapped;
}

template <typename T>
inline void se2Inverse(const T* pose, T* out) {
  const T c = cos(pose[2]);
  const T s = sin(pose[2]);
  out[0] = -(c * pose[0] + s * pose[1]);
  out[1] = -(-s * pose[0] + c * pose[1]);
  out[2] = normalizeAngle(-pose[2]);
}

// out = a (*) b in SE(2), composition of rigid transforms.
template <typename T>
inline void se2Compose(const T* a, const T* b, T* out) {
  const T c = cos(a[2]);
  const T s = sin(a[2]);
  out[0] = a[0] + c * b[0] - s * b[1];
  out[1] = a[1] + s * b[0] + c * b[1];
  out[2] = normalizeAngle(a[2] + b[2]);
}

}  // namespace pgo

#endif
