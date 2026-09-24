// Rigid-transform math built on Eigen: composition, inversion and
// shortest-arc quaternion interpolation (SLERP).
#pragma once

#include <Eigen/Dense>
#include <Eigen/Geometry>

#include <algorithm>
#include <cmath>
#include <string>

namespace tf {

using Vector3 = Eigen::Vector3d;
using Quaternion = Eigen::Quaterniond;
using AngleAxisd = Eigen::AngleAxisd;
using Matrix3 = Eigen::Matrix3d;
using Matrix4 = Eigen::Matrix4d;

// T_b_from_a: maps coordinates expressed in frame `a` into frame `b`
// when `b` is a child of `a`. t is the child origin measured in the parent,
// q rotates parent vectors into child orientation convention.
struct Transform {
  Vector3 t = Vector3::Zero();
  Quaternion q = Quaternion::Identity();

  Matrix4 matrix() const {
    Matrix4 m = Matrix4::Identity();
    m.topLeftCorner<3, 3>() = q.toRotationMatrix();
    m.topRightCorner<3, 1>() = t;
    return m;
  }
};

// a * b means: apply b first, then a (standard homogeneous composition).
inline Transform compose(const Transform& a, const Transform& b) {
  Transform out;
  out.q = a.q * b.q;
  out.q.normalize();
  out.t = a.t + a.q * b.t;
  return out;
}

// Exact inverse of a rigid transform: R^T, t' = -R^T t.
inline Transform inverse(const Transform& x) {
  Transform out;
  out.q = x.q.conjugate();
  out.t = -(out.q * x.t);
  return out;
}

// Spherical linear interpolation along the short great-circle arc.
// Handles the antipodal-sign ambiguity (dot < 0) and the near-parallel case.
inline Quaternion slerpShortest(const Quaternion& qa, const Quaternion& qb,
                                double u) {
  Quaternion a = qa.normalized();
  Quaternion b = qb.normalized();
  double d = a.dot(b);
  if (d < 0.0) {  // pick the equivalent representation giving the short arc
    b.coeffs() = -b.coeffs();
    d = -d;
  }
  d = std::clamp(d, -1.0, 1.0);

  // Nearly identical orientations: LERP + normalize is numerically safer
  // than dividing by sin(theta0), and exact at u = 0, 1.
  if (d > 0.999995) {
    Quaternion r;
    r.coeffs() = a.coeffs() + u * (b.coeffs() - a.coeffs());
    r.normalize();
    return r;
  }

  const double theta0 = std::acos(d);
  const double sin_theta0 = std::sin(theta0);
  const double theta = theta0 * u;
  const double s0 = std::cos(theta) - d * std::sin(theta) / sin_theta0;
  const double s1 = std::sin(theta) / sin_theta0;

  Quaternion r;
  r.coeffs() = s0 * a.coeffs() + s1 * b.coeffs();
  r.normalize();
  return r;
}

// Translation: linear. Rotation: shortest-arc SLERP.
inline Transform interpolate(const Transform& a, const Transform& b,
                             double u) {
  Transform out;
  out.t = a.t + u * (b.t - a.t);
  out.q = slerpShortest(a.q, b.q, u);
  return out;
}

struct QuaternionCheck {
  bool ok = false;
  std::string error;
};

// Components are in [w, x, y, z] order. Rejects non-finite components,
// zero/near-zero norm and norms too far from unit. On success the caller
// receives a normalized quaternion.
inline QuaternionCheck validateQuaternion(double w, double x, double y,
                                          double z, Quaternion* normalized,
                                          double norm_tol = 1e-3) {
  const double comps[4] = {w, x, y, z};
  for (double c : comps) {
    if (!std::isfinite(c)) {
      return {false, "quaternion contains non-finite (NaN/Inf) component"};
    }
  }
  const double norm2 = w * w + x * x + y * y + z * z;
  if (norm2 <= 0.0) {
    return {false, "quaternion norm is zero"};
  }
  const double norm = std::sqrt(norm2);
  if (std::fabs(norm - 1.0) > norm_tol) {
    return {false, "quaternion is not unit norm (|q|=" +
                       std::to_string(norm) + ")"};
  }
  if (normalized != nullptr) {
    *normalized = Quaternion(w / norm, x / norm, y / norm, z / norm);
  }
  return {true, ""};
}

}  // namespace tf
