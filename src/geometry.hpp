// Geometry primitives: 3D vector, unit quaternion, and SLERP.
//
// Conventions:
//   - Right-handed coordinate system, positions in meters.
//   - Quaternion layout (w, x, y, z), representing a rotation of a vector
//     from the body frame into the reference (world) frame.
//   - q and -q denote the same rotation (sign equivalence); SLERP always
//     takes the shortest path by flipping the sign of the second operand
//     when the dot product is negative.
#pragma once

#include <cmath>
#include <stdexcept>

namespace traj {

struct Vec3 {
    double x = 0.0, y = 0.0, z = 0.0;

    Vec3 operator+(const Vec3& o) const { return {x + o.x, y + o.y, z + o.z}; }
    Vec3 operator*(double s) const { return {x * s, y * s, z * s}; }
};

inline Vec3 lerp(const Vec3& a, const Vec3& b, double u) {
    return a * (1.0 - u) + b * u;
}

struct Quat {
    double w = 1.0, x = 0.0, y = 0.0, z = 0.0;

    double norm() const { return std::sqrt(w * w + x * x + y * y + z * z); }

    Quat normalized() const {
        double n = norm();
        if (n == 0.0) throw std::invalid_argument("zero-norm quaternion");
        return {w / n, x / n, y / n, z / n};
    }

    Quat operator-() const { return {-w, -x, -y, -z}; }
    Quat operator+(const Quat& o) const { return {w + o.w, x + o.x, y + o.y, z + o.z}; }
    Quat operator*(double s) const { return {w * s, x * s, y * s, z * s}; }
};

inline double dot(const Quat& a, const Quat& b) {
    return a.w * b.w + a.x * b.x + a.y * b.y + a.z * b.z;
}

// Threshold on the (post sign-flip) dot product above which the two
// quaternions are treated as nearly identical and SLERP degrades to
// normalized linear interpolation (NLERP). dot > cos(sqrt(2*1e-12)) ~ 1-1e-12
// corresponds to a rotation angle below ~2 microradians.
constexpr double kSlerpLinearThreshold = 1.0 - 1e-12;

// Spherical linear interpolation between unit quaternions a and b.
// Inputs are normalized defensively; u in [0,1] (callers may clamp first).
// Guarantees: result is unit norm (within rounding), slerp(a,b,0)==a',
// slerp(a,b,1)==±b' with the sign chosen for shortest-path continuity.
inline Quat slerp(const Quat& qa, const Quat& qb, double u) {
    Quat a = qa.normalized();
    Quat b = qb.normalized();

    double d = dot(a, b);
    if (d < 0.0) {  // sign equivalence: take the shortest path
        b = -b;
        d = -d;
    }
    if (d > 1.0) d = 1.0;  // guard against rounding

    if (d > kSlerpLinearThreshold) {
        // Small-angle / antipodal-after-flip degenerate case: linear
        // interpolation plus renormalization (exact for d == 1).
        return (a * (1.0 - u) + b * u).normalized();
    }

    double theta = std::acos(d);
    double sinTheta = std::sin(theta);
    double wa = std::sin((1.0 - u) * theta) / sinTheta;
    double wb = std::sin(u * theta) / sinTheta;
    return a * wa + b * wb;  // unit norm by construction
}

}  // namespace traj
